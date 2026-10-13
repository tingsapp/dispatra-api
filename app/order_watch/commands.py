"""Authorized application commands used by the operational agent and human endpoints."""
from types import SimpleNamespace
from contextlib import contextmanager
from uuid import uuid4
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select,text
from app.models import now
from app.database import context
from app.services import audit, once
from app.operations.common import company_lock, record, version
from app.operations.models import Order, Route
from app.operations.orders import company_scope
from .models import Incident, Question
from .schemas import CHOICES
from .evidence import fact
from .playbooks import suggestions,advice_for_reason

OPEN = ('OPEN','ACKNOWLEDGED')


def system(organization_id):
    return SimpleNamespace(id=None,role='OPERATIONAL',organization_id=organization_id,shipper_id=None,driver_id=None,actor_type='AGENT')


def touch(db, actor, row, action):
    row.version += 1; row.updated_at = now()
    db.flush(); audit(db,actor,action,row.id,actor.organization_id)


def apply(db, actor, order, signal):
    if actor.role != 'OPERATIONAL': raise HTTPException(403,'Operational agent command required.')
    if order.organization_id != actor.organization_id or order.status in {'COMPLETED','CANCELLED'}: return None
    row = db.scalar(select(Incident).where(Incident.organization_id==actor.organization_id, Incident.fingerprint==signal['fingerprint']).with_for_update())
    if row is None:
        row = Incident(id=uuid4(),organization_id=actor.organization_id,order_id=order.id,**signal)
        db.add(row); db.flush(); audit(db,actor,'watch.detected',row.id,actor.organization_id)
    elif row.source_issue_id is not None:
        return row  # A factual timing refresh cannot overwrite or reopen a human-reported issue.
    elif row.status in ('RESOLVED','SUPERSEDED'):
        row.status='OPEN';row.resolved_at=None;row.resolution='';row.acknowledged_at=None
        for field,value in signal.items():setattr(row,field,value)
        touch(db,actor,row,'watch.detected')
    elif row.status in OPEN:
        # Driver explanations remain reported evidence; fresh timing observations cannot erase them.
        updates = {key:value for key,value in signal.items() if key not in ('fingerprint','source_issue_id','stop_id')}
        if row.certainty=='REPORTED' and not row.source_issue_id:
            updates.pop('certainty',None); updates.pop('description',None)
            question=db.scalar(select(Question).where(Question.organization_id==actor.organization_id,Question.incident_id==row.id))
            updates['suggestions']=advice_for_reason(question.reason) if question and question.status=='ANSWERED' else row.suggestions
            updates['evidence'] = [e for e in row.evidence if e['source'] in {'Driver reply','Driver estimate and stop execution'}] + signal['evidence']
        changed = any(getattr(row,key)!=value for key,value in updates.items())
        urgent = row.severity != signal['severity'] and signal['severity']=='CRITICAL'
        if changed:
            for key,value in updates.items(): setattr(row,key,value)
            touch(db,actor,row,'watch.escalated' if urgent else 'watch.updated')
    return row


def close(db, actor, row, explanation, superseded=False):
    if row.status not in OPEN: return
    row.status='SUPERSEDED' if superseded else 'RESOLVED'
    row.resolved_at, row.resolution=now(),explanation
    touch(db,actor,row,'watch.resolved')
    question=db.scalar(select(Question).where(Question.organization_id==actor.organization_id,Question.incident_id==row.id))
    if question and question.status=='OPEN':
        question.status='CLOSED'; question.version+=1; question.updated_at=now()


def ask(db, actor, row, route, prompt):
    if route is None or route.status not in {'PLANNED','IN_PROGRESS'}: return
    existing=db.scalar(select(Question.id).where(Question.organization_id==actor.organization_id,Question.incident_id==row.id))
    if existing: return
    q=Question(id=uuid4(),organization_id=actor.organization_id,incident_id=row.id,order_id=row.order_id,
        route_id=route.id,driver_id=route.driver_id,generation=route.generation,prompt=prompt)
    db.add(q);db.flush();audit(db,actor,'watch.question_created',q.id,actor.organization_id)


@contextmanager
def report_scope(db,actor):
    db.flush()
    saved=db.execute(text("SELECT current_setting('app.organization_id',true), current_setting('app.platform',true), current_setting('app.shipper_id',true), current_setting('app.driver_id',true)")).one()
    context(db,actor.organization_id)
    try:yield
    finally:
        if db.is_active:context(db,saved[0] or None,saved[1]=='true',saved[2] or None,saved[3] or None)


def reported(db, actor, issue, monitor_incident_id=None):
    # The existing issue-report use case has already authorized the reporter and validated the Order/stop.
    with report_scope(db,actor):
        agent=system(actor.organization_id)
        order=record(db,Order,agent,issue.order_id,True)
        signal=dict(fingerprint=f'reported:{issue.id}',source_issue_id=issue.id,stop_id=issue.stop_id,kind='REPORTED',
            title='Operational issue reported', description=issue.description[:1000],severity='CRITICAL',certainty='REPORTED',
            evidence=[fact('Reported issue',issue.kind,'Operational issue report',issue.created_at)],
            suggestions=suggestions('REPORTED',issue.kind),response_due_at=now())
        # issue.reported already notifies owner/dispatch; avoid a second notification for the same report.
        if monitor_incident_id:
            row=record(db,Incident,agent,monitor_incident_id,True)
            if row.order_id!=order.id:raise HTTPException(409,'Operational report does not match this order.')
            row.source_issue_id=issue.id
            row.kind,row.title='REPORTED','Operational issue reported'
            row.description=issue.description[:1000]
            row.certainty,row.severity='REPORTED','CRITICAL'
            row.suggestions=suggestions('REPORTED',issue.kind)
            db.flush()
            return row
        row=db.scalar(select(Incident).where(Incident.organization_id==agent.organization_id,Incident.source_issue_id==issue.id))
        if row is None:
            row=Incident(organization_id=agent.organization_id,order_id=order.id,**signal)
            db.add(row);db.flush();audit(db,agent,'watch.recorded',row.id,agent.organization_id)
        return row


def question_view(db,row):
    order=db.get(Order,row.order_id)
    return jsonable_encoder(dict(id=row.id,version=row.version,order_id=row.order_id,order_number=order.number if order else '',prompt=row.prompt,
        choices=CHOICES,status=row.status,reason=row.reason,details=row.details,estimated_ready_at=row.estimated_ready_at,
        created_at=row.created_at,answered_at=row.answered_at))


def own_question(db, actor, identity, lock=False):
    q=record(db,Question,actor,identity,lock)
    order=record(db,Order,actor,q.order_id,True)
    route=record(db,Route,actor,q.route_id,True)
    if actor.role!='DRIVER' or q.driver_id!=actor.driver_id or route.driver_id!=actor.driver_id or order.route_id!=route.id:
        raise HTTPException(404,'Question not found.')
    if route.generation!=q.generation or order.status in {'COMPLETED','CANCELLED'} or route.status not in {'PLANNED','IN_PROGRESS'}:
        raise HTTPException(409,'This question is no longer current. Refresh your order.')
    return q


def reply(db, actor, identity, data, key):
    def run():
        company_lock(db,actor)
        q=own_question(db,actor,identity,True);version(q,data.version)
        if q.status!='OPEN':raise HTTPException(409,'This question is already answered or closed.')
        current=now()
        if data.estimated_ready_at and data.estimated_ready_at<current:
            raise HTTPException(422,'Expected ready time must be in the future.')
        q.status,q.reason,q.details='ANSWERED',data.reason,data.details.strip()
        q.estimated_ready_at,q.answered_at=data.estimated_ready_at,current
        q.version+=1;q.updated_at=current;db.flush()
        with company_scope(db,actor):
            row=record(db,Incident,actor,q.incident_id,True)
            label=next(choice['label'] for choice in CHOICES if choice['value']==data.reason)
            row.certainty='REPORTED'
            row.description=f'Driver reports: {label}.'+(f' {q.details}' if q.details else '')
            row.description=row.description[:1000]
            row.evidence=[fact('Driver explanation',label,'Driver reply',current)]+([fact('Driver note',q.details,'Driver reply',current)] if q.details else [])+row.evidence
            if q.estimated_ready_at:
                row.evidence.insert(1,fact('Driver expected ready time',q.estimated_ready_at.isoformat(),'Driver reply',current))
                from .evidence import instant
                orders=db.scalars(select(Order).where(Order.organization_id==actor.organization_id,Order.route_id==q.route_id,Order.status.in_(('ASSIGNED','IN_PROGRESS')))).all()
                from app.operations.models import RouteStop
                upcoming=set(str(identity) for identity in db.scalars(select(RouteStop.stop_id).where(RouteStop.organization_id==actor.organization_id,RouteStop.route_id==q.route_id,RouteStop.status!='COMPLETED',RouteStop.arrived_at.is_(None))))
                threatened=[o.number for o in orders if any(stop['id'] in upcoming and instant(stop.get('window_end')) and instant(stop['window_end'])<q.estimated_ready_at for stop in o.facts['stops'])]
                if threatened:
                    row.severity='CRITICAL'
                    row.evidence.insert(2,fact('Commitments threatened by reported ready time',', '.join(threatened),'Driver estimate compared with booked windows',current))
            # Acknowledgement/explanation is not proof of recovery. Only objective progress or explicit issue resolution closes it.
            reason_kind={'VEHICLE_PROBLEM':'VEHICLE','UNSAFE':'UNSAFE','RECIPIENT_UNAVAILABLE':'FAILED_DELIVERY','ACCESS_BLOCKED':'WRONG_ADDRESS'}.get(data.reason)
            row.suggestions=advice_for_reason(data.reason)
            if data.reason in {'UNSAFE','DAMAGED_ITEM','VEHICLE_PROBLEM'}:row.severity='CRITICAL'
            if data.reason=='NO_PROBLEM':row.suggestions=['Verify that the current milestone is recorded and continue monitoring the booked commitments.']
            if data.reason in {'UNSAFE','VEHICLE_PROBLEM','DAMAGED_ITEM'}:
                from app.operations.issues import report
                from app.operations.schemas import IssueInput
                issue_kind={'UNSAFE':'UNSAFE','VEHICLE_PROBLEM':'VEHICLE','DAMAGED_ITEM':'OTHER'}[data.reason]
                report(db,actor,q.order_id,IssueInput(stop_id=row.stop_id,kind=issue_kind,description=row.description),
                    f'watch-report-{q.id}',monitor_incident_id=row.id)
                touch(db,actor,row,'watch.updated')
            else:touch(db,actor,row,'watch.driver_replied')
        return question_view(db,q)
    # Check current assignment even on an idempotent replay; released drivers lose access.
    own_question(db,actor,identity)
    return once(db,actor,key,f'watch-reply:{identity}',data.model_dump(mode='json'),run)


def acknowledge(db,actor,identity,data,key):
    def run():
        if actor.role=='ADMIN':row=db.scalar(select(Incident).where(Incident.id==identity).with_for_update())
        elif actor.role=='DISPATCHER':row=record(db,Incident,actor,identity,True)
        else:raise HTTPException(403,'Dispatcher or platform owner required.')
        if row is None:raise HTTPException(404,'Operational issue not found.')
        version(row,data.version)
        if row.status not in OPEN:raise HTTPException(409,'This operational issue is closed.')
        row.status='ACKNOWLEDGED';row.acknowledged_at=now()
        row.version+=1;row.updated_at=now()
        audit(db,actor,'platform_owner.operational_acknowledged' if actor.role=='ADMIN' else 'watch.acknowledged',row.id,row.organization_id)
        db.flush()
        from .queries import incident_view
        return incident_view(db,row)
    return once(db,actor,key,f'watch-ack:{identity}',data.model_dump(),run)


def request_review(db,owner,key):
    from datetime import timedelta
    from sqlalchemy import or_,and_
    from app.agents.models import AgentRun
    from app.agents.schemas import AgentRunView
    def run():
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('order-watch-request',0))"))
        row=db.scalar(select(AgentRun).where(AgentRun.agent=='OPERATIONAL',or_(AgentRun.status=='QUEUED',
            and_(AgentRun.status=='RUNNING',AgentRun.heartbeat_at>now()-timedelta(minutes=5)))).order_by(AgentRun.created_at.desc()))
        if row is None:
            row=AgentRun(agent='OPERATIONAL',status='QUEUED',step='Waiting for operational order review',progress=0)
            db.add(row);db.flush();audit(db,owner,'platform_owner.order_review_requested',row.id,None)
        return AgentRunView.model_validate(row).model_dump(mode='json')
    return once(db,owner,key,'order-watch-request',{},run)


def follow_up_ready_time(db,actor,row,route,current):
    if row.source_issue_id or row.status not in OPEN or route is None:return
    q=db.scalar(select(Question).where(Question.organization_id==actor.organization_id,Question.incident_id==row.id))
    if not q or q.status!='ANSWERED' or not q.estimated_ready_at or q.estimated_ready_at>current:return
    if q.route_id!=route.id or q.generation!=route.generation:return
    previous=q.estimated_ready_at
    q.status='OPEN';q.version+=1;q.updated_at=current
    q.prompt='Your estimated readiness time has passed, but this stop is still incomplete. When safely stopped, update your progress and expected ready time.'
    row.description=(row.description+' The reported ready time has passed; a progress update has been requested.')[:1000]
    row.evidence=[fact('Reported ready time passed without stop completion',previous.isoformat(),'Driver estimate and stop execution',current)]+row.evidence
    touch(db,actor,row,'watch.update_requested')
    db.flush();audit(db,actor,'watch.question_created',q.id,actor.organization_id)
