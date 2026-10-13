from sqlalchemy import select
from app.models import now
from app.operations.models import Order,OrderStop,Route,RouteStop,Issue,DutySession
from app.operations.common import company_lock
from app.dispatch.models import DispatchDecision
from .models import Incident,Question,RouteEstimate
from .commands import OPEN,apply,ask,close,reported,follow_up_ready_time
from .detector import evaluate
from .evidence import instant


def scan_company(db,actor):
    company_lock(db,actor)
    current=now()
    orders=db.scalars(select(Order).where(Order.organization_id==actor.organization_id,Order.status.in_(('NEW','ASSIGNED','IN_PROGRESS'))).with_for_update()).all()
    routes={r.id:r for r in db.scalars(select(Route).where(Route.organization_id==actor.organization_id,Route.status.in_(('PLANNED','IN_PROGRESS'))))}
    visits=db.scalars(select(RouteStop).where(RouteStop.organization_id==actor.organization_id,RouteStop.route_id.in_(routes)).order_by(RouteStop.position)).all()
    stops={s.id:s for s in db.scalars(select(OrderStop).where(OrderStop.organization_id==actor.organization_id,OrderStop.id.in_([v.stop_id for v in visits])))}
    # Unassigned Orders have no RouteStops yet.
    for s in db.scalars(select(OrderStop).where(OrderStop.organization_id==actor.organization_id,OrderStop.order_id.in_([o.id for o in orders]))):stops[s.id]=s
    estimates={e.route_id:e for e in db.scalars(select(RouteEstimate).where(RouteEstimate.organization_id==actor.organization_id))}
    decisions=db.scalars(select(DispatchDecision).where(DispatchDecision.organization_id==actor.organization_id,DispatchDecision.order_id.in_([o.id for o in orders])).order_by(DispatchDecision.created_at.desc())).all()
    reports=db.scalars(select(Issue).where(Issue.organization_id==actor.organization_id,Issue.resolved.is_(False))).all()
    for issue in reports:reported(db,actor,issue)
    seen=set()
    for order in orders:
        route=routes.get(order.route_id);route_visits=[v for v in visits if v.route_id==order.route_id]
        estimate=estimates.get(order.route_id)
        next_visit=next((v for v in route_visits if v.status!='COMPLETED'),None)
        duty=db.scalar(select(DutySession).where(DutySession.organization_id==actor.organization_id,DutySession.driver_id==route.driver_id,DutySession.ended_at.is_(None))) if route else None
        if estimate and (not route or not duty or estimate.captured_at<duty.started_at or estimate.data.get('duty_id') and estimate.data['duty_id']!=str(duty.id) or estimate.generation!=route.generation or estimate.route_version!=route.version
            or not next_visit or estimate.next_stop_id!=next_visit.stop_id or next_visit.arrived_at is not None
            or (current-estimate.captured_at).total_seconds()>120 or (current-estimate.observed_at).total_seconds()>180):estimate=None
        decision=next((d for d in decisions if d.order_id==order.id and d.order_version==order.version),None)
        if decision and decision.status!='NO_CANDIDATE':decision=None
        for observation in evaluate(order,route,route_visits,stops,estimate,current,decision):
            row=apply(db,actor,order,observation);seen.add(row.id)
            if row.certainty=='REPORTED':follow_up_ready_time(db,actor,row,route,current)
            if row.status in OPEN and row.certainty=='NEEDS_CONFIRMATION':
                ask(db,actor,row,route,f'Order {order.number}: {row.description} When safely stopped, tell us what is happening. Select a reason or describe the situation, and give an expected ready time if known.')
    reconcile(db,actor,seen)
    return len(orders)


def reconcile(db,actor,seen):
    for row in db.scalars(select(Incident).where(Incident.organization_id==actor.organization_id,Incident.status.in_(OPEN)).with_for_update()):
        order=db.get(Order,row.order_id)
        if row.source_issue_id:
            source=db.get(Issue,row.source_issue_id)
            if source and source.resolved:close(db,actor,row,'The reported operational issue was explicitly resolved.')
            continue
        if not order or order.status in {'COMPLETED','CANCELLED'}:
            close(db,actor,row,'Order closed; this monitoring observation is no longer active.',superseded=True);continue
        if row.id in seen:continue
        if row.kind=='UNASSIGNED':
            if order.route_id:close(db,actor,row,'A valid route assignment is recorded.');continue
            if row.fingerprint!=f'unassigned:{order.id}:{order.version}':
                close(db,actor,row,'The booking was revised; this observation belongs to the previous commitment.',superseded=True);continue
        route=db.get(Route,order.route_id) if order.route_id else None
        question=db.scalar(select(Question).where(Question.organization_id==actor.organization_id,Question.incident_id==row.id))
        if question and (not route or question.route_id!=route.id or question.generation!=route.generation):
            close(db,actor,row,'The assignment changed; this observation belongs to the previous route.',superseded=True);continue
        if row.kind=='START_OVERDUE' and route and route.started_at:close(db,actor,row,'Route start is recorded.');continue
        if row.stop_id:
            visit=db.scalar(select(RouteStop).where(RouteStop.organization_id==actor.organization_id,RouteStop.stop_id==row.stop_id,
                RouteStop.route_id==order.route_id))
            if not visit:close(db,actor,row,'Assignment or published visit changed; review the new plan.',superseded=True);continue
            if visit.status=='COMPLETED' or row.kind in {'ARRIVAL_OVERDUE','ARRIVAL_RISK','TRAFFIC_RISK'} and visit.arrived_at:
                close(db,actor,row,'Recorded stop progress verifies this timing observation has ended.');continue
            if row.kind in {'ARRIVAL_RISK','TRAFFIC_RISK'}:
                estimate=db.scalar(select(RouteEstimate).where(RouteEstimate.organization_id==actor.organization_id,RouteEstimate.route_id==order.route_id))
                first=db.scalar(select(RouteStop).where(RouteStop.organization_id==actor.organization_id,RouteStop.route_id==order.route_id,RouteStop.status!='COMPLETED').order_by(RouteStop.position).limit(1))
                duty=db.scalar(select(DutySession).where(DutySession.organization_id==actor.organization_id,DutySession.driver_id==route.driver_id,DutySession.ended_at.is_(None))) if route else None
                fresh=estimate and route and duty and estimate.captured_at>=duty.started_at and first and first.arrived_at is None and estimate.generation==route.generation and estimate.route_version==route.version and estimate.next_stop_id==first.stop_id and (now()-estimate.captured_at).total_seconds()<=120 and (now()-estimate.observed_at).total_seconds()<=180
                if fresh:
                    prediction=next((item for item in estimate.data['stops'] if item['stop_id']==str(row.stop_id)),None)
                    from .evidence import deadline
                    stop=db.get(OrderStop,row.stop_id)
                    due=deadline(order,stop) if stop else None
                    if prediction and due and instant(prediction['eta'])<=due:
                        close(db,actor,row,'A current qualified estimate fits the booked arrival window.');continue
                # Missing estimates never prove recovery; preserve the incident with explicit uncertainty.
                if row.certainty!='NEEDS_CONFIRMATION':
                    row.certainty='NEEDS_CONFIRMATION';row.description='A previous arrival prediction needs review. No current qualified estimate confirms recovery.'
                    from .commands import touch
                    touch(db,actor,row,'watch.updated')
    for q in db.scalars(select(Question).where(Question.organization_id==actor.organization_id,Question.status=='OPEN')):
        order=db.get(Order,q.order_id);route=db.get(Route,q.route_id)
        if not order or order.route_id!=q.route_id or not route or route.generation!=q.generation or order.status in {'COMPLETED','CANCELLED'}:
            q.status='CLOSED';q.version+=1;q.updated_at=now()
