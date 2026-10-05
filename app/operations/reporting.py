from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_, func
from app import auth
from app.models import User, now
from app.routes import DB
from .models import Order, Shipper, Driver, Vehicle, Catalog, Route, Location, DutySession, Issue, RouteStop, OrderStop
from .schemas import AnalyticsView, DriverActivityView, MonitorView
from .common import record
from .pricing import money
from .attention import flags as attention_flags

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Operations reporting'])


def dated_orders(db, user, date_from, date_to):
    if (date_from and date_from.tzinfo is None) or (date_to and date_to.tzinfo is None):
        raise HTTPException(422,'Date filters must include a time zone.')
    if date_from and date_to and date_from >= date_to: raise HTTPException(422,'Date range must end after it starts.')
    query = select(Order).where(Order.organization_id == user.organization_id)
    if date_from: query = query.where(Order.scheduled_at >= date_from)
    if date_to: query = query.where(Order.scheduled_at < date_to)
    return list(db.scalars(query))


@router.get('/analytics', response_model=AnalyticsView)
def analytics(slug: str, db: DB, date_from: datetime | None = None, date_to: datetime | None = None, user: User = Depends(auth.dispatcher)):
    orders = dated_orders(db,user,date_from,date_to)
    statuses = Counter(o.status for o in orders)
    daily = defaultdict(lambda: {'orders':0,'completed':0,'revenue':Decimal(0)})
    from zoneinfo import ZoneInfo
    from .common import settings
    _, config = settings(db,user)
    for order in orders:
        day = order.scheduled_at.astimezone(ZoneInfo(config.time_zone)).date().isoformat()
        daily[day]['orders'] += 1
        if order.status in {'COMPLETED','INVOICED'}:
            daily[day]['completed'] += 1
            if order.pricing.get('subtotal') is not None: daily[day]['revenue'] += Decimal(order.pricing['subtotal'])
    finished = [o for o in orders if o.status in {'COMPLETED','INVOICED'}]
    hourly = Counter(o.completed_at.astimezone(ZoneInfo(config.time_zone)).hour for o in finished if o.completed_at)
    accessorials = Counter(a.get('code',a['name']) for o in finished for a in o.pricing.get('context',{}).get('accessorials',[]))
    visits = list(db.execute(select(RouteStop,OrderStop.order_id).join(OrderStop,OrderStop.id == RouteStop.stop_id).where(
        RouteStop.organization_id == user.organization_id,OrderStop.order_id.in_([o.id for o in finished]),RouteStop.status == 'COMPLETED'))) if finished else []
    observed = {str(v.stop_id):v for v,_ in visits}
    variances = [(v.arrived_at-v.planned_at).total_seconds()/60 for v,_ in visits if v.arrived_at]
    shipper_ids = {o.shipper_id for o in orders}
    service_ids = {o.service_id for o in orders}
    route_ids = {o.route_id for o in orders if o.route_id}
    shippers = {row.id: row for row in db.scalars(select(Shipper).where(Shipper.organization_id == user.organization_id, Shipper.id.in_(shipper_ids)))} if shipper_ids else {}
    services = {row.id: row for row in db.scalars(select(Catalog).where(Catalog.organization_id == user.organization_id, Catalog.id.in_(service_ids)))} if service_ids else {}
    routes = {row.id: row for row in db.scalars(select(Route).where(Route.organization_id == user.organization_id, Route.id.in_(route_ids)))} if route_ids else {}
    driver_ids = {r.driver_id for r in routes.values()}
    vehicle_ids = {r.vehicle_id for r in routes.values()}
    drivers = {row.id: row for row in db.scalars(select(Driver).where(Driver.organization_id == user.organization_id, Driver.id.in_(driver_ids)))} if driver_ids else {}
    vehicles = {row.id: row for row in db.scalars(select(Vehicle).where(Vehicle.organization_id == user.organization_id, Vehicle.id.in_(vehicle_ids)))} if vehicle_ids else {}
    rows = []
    known_sla = []
    for order in orders:
        drops = [s for s in order.facts['stops'] if s['kind'] == 'DROPOFF']
        last = drops[-1] if drops else None
        visit = observed.get(last['id']) if last else None
        arrived = visit.arrived_at if visit else None
        window_start = datetime.fromisoformat(last['window_start']) if last and last.get('window_start') else None
        window_end = datetime.fromisoformat(last['window_end']) if last and last.get('window_end') else None
        finished_order = order.status in {'COMPLETED', 'INVOICED'}
        known = finished_order and bool(drops) and all(s.get('window_end') and s['id'] in observed and observed[s['id']].completed_at for s in drops)
        ontime = known and all(observed[s['id']].completed_at <= datetime.fromisoformat(s['window_end']) for s in drops)
        if known: known_sla.append(ontime)
        sla_status = 'UNKNOWN'
        if known and window_end:
            sla_status = 'LATE' if visit.completed_at > window_end else 'AHEAD' if window_start and arrived and arrived < window_start else 'ON_TIME'
        variance = round((arrived-window_end).total_seconds()/60,2) if arrived and window_end else None
        day = order.scheduled_at.astimezone(ZoneInfo(config.time_zone)).date().isoformat()
        if known:
            daily[day]['sla_known'] = daily[day].get('sla_known', 0) + 1
            key = 'on_time' if ontime else 'late'
            daily[day][key] = daily[day].get(key, 0) + 1
        route = routes.get(order.route_id)
        driver = drivers.get(route.driver_id) if route else None
        vehicle = vehicles.get(route.vehicle_id) if route else None
        rows.append({'id':str(order.id),'number':order.number,'scheduled_at':order.scheduled_at.isoformat(),
            'status':order.status,'total':order.pricing.get('total'),
            'shipper_name':shippers[order.shipper_id].name if order.shipper_id in shippers else '',
            'driver_name':driver.name if driver else '', 'driver_number':driver.number if driver else '',
            'vehicle_unit':vehicle.data.get('unit_number','') if vehicle else '',
            'service_name':services[order.service_id].data.get('name','') if order.service_id in services else '',
            'window_start':window_start,'window_end':window_end,'actual_arrival':arrived,
            'sla_status':sla_status,'variance_minutes':variance,
            'accessorials':[a.get('name',a.get('code','')) for a in order.pricing.get('context',{}).get('accessorials',[])],
            'pod_verified':finished_order and bool(drops) and all(s['id'] in observed for s in drops)})
    return {'hourly_completed':[{'hour':h,'orders':hourly[h]} for h in range(24)],
        'accessorial_counts':dict(accessorials),'pod_verified_orders':len(finished),
        'on_time_orders':sum(known_sla),'sla_known_orders':len(known_sla),
        'on_time_percent':round(sum(known_sla)/len(known_sla)*100,1) if known_sla else None,
        'average_arrival_variance_minutes':round(sum(variances)/len(variances),2) if variances else None,
        'orders':len(orders), 'statuses':dict(statuses), 'completed_orders':sum(d['completed'] for d in daily.values()),
        'completed_revenue_before_tax':str(money(sum((d['revenue'] for d in daily.values()),Decimal(0)))),
        'daily':[{'date':day,**values,'revenue':str(money(values['revenue']))} for day,values in sorted(daily.items())],
        'rows':rows}


@router.get('/drivers/{identity}/activity', response_model=DriverActivityView)
def driver_activity(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    driver = record(db,Driver,user,identity)
    completed = list(db.scalars(select(Order).join(Route,Route.id == Order.route_id).where(Order.organization_id == user.organization_id,
        Route.driver_id == identity,Order.status.in_(['COMPLETED','INVOICED']))))
    current = db.scalar(select(Route).where(Route.organization_id == user.organization_id,Route.driver_id == identity,Route.status.in_(['PLANNED','IN_PROGRESS'])))
    point = db.scalar(select(Location).join(DutySession,DutySession.id == Location.duty_id).where(Location.organization_id == user.organization_id,
        Location.driver_id == identity,or_(DutySession.ended_at.is_(None),Location.captured_at <= DutySession.ended_at)).order_by(Location.captured_at.desc()).limit(1))
    return {'completed_orders':len(completed),
        'app_connectivity': 'UNKNOWN' if driver.last_seen_at is None else 'CONNECTED' if now()-driver.last_seen_at < timedelta(minutes=2) else 'STALE',
        'app_last_seen':driver.last_seen_at,'gps_captured':point.captured_at if point else None,
        'location_permission':driver.location_permission,'current_route':str(current.id) if current else None}


@router.get('/monitor', response_model=MonitorView)
def monitor(slug: str, db: DB, user: User = Depends(auth.dispatcher)):
    routes = list(db.scalars(select(Route).where(Route.organization_id == user.organization_id,Route.status.in_(['PLANNED','IN_PROGRESS']))))
    orders = list(db.scalars(select(Order).where(Order.organization_id == user.organization_id,Order.status.in_(['NEW','ASSIGNED','IN_PROGRESS']))))
    invoice_holds = list(db.scalars(select(Order).where(Order.organization_id == user.organization_id, Order.status == 'COMPLETED')))
    issues = list(db.scalars(select(Issue).where(Issue.organization_id == user.organization_id,Issue.resolved.is_(False))))
    drivers = list(db.scalars(select(Driver).where(Driver.organization_id == user.organization_id,Driver.active.is_(True))))
    route_ids = [route.id for route in routes]
    visits = db.scalars(select(RouteStop).where(RouteStop.organization_id == user.organization_id,
        RouteStop.route_id.in_(route_ids))).all() if route_ids else []
    visits_by_stop = {str(visit.stop_id): visit for visit in visits}
    latest = select(Location.id.label('id'), func.row_number().over(
        partition_by=Location.driver_id,
        order_by=(Location.captured_at.desc(), Location.id.desc())).label('rank')).join(
        DutySession, DutySession.id == Location.duty_id).where(
        Location.organization_id == user.organization_id,
        DutySession.organization_id == user.organization_id,
        DutySession.ended_at.is_(None)).subquery()
    points = {point.driver_id: point for point in db.scalars(select(Location).join(
        latest, Location.id == latest.c.id).where(latest.c.rank == 1))}
    active_duty_ids = set(db.scalars(select(DutySession.driver_id).where(
        DutySession.organization_id == user.organization_id,
        DutySession.ended_at.is_(None))))
    observed_at = now()
    attention = {order.id: attention_flags(order, visits_by_stop, observed_at) for order in orders}
    return {'drivers':[{'id':str(d.id),'name':d.name,'vehicle_id':str(d.vehicle_id) if d.vehicle_id else None,
        'on_duty':d.id in active_duty_ids,
        'location':points[d.id].data if d.id in points else None,
        'gps_status':'UNKNOWN' if d.id not in points else 'FRESH' if observed_at-points[d.id].captured_at < timedelta(minutes=2) else 'STALE',
        'app_last_seen':d.last_seen_at,'location_permission':d.location_permission} for d in drivers],
        'routes':[{'id':str(r.id),'driver_id':str(r.driver_id),'vehicle_id':str(r.vehicle_id),'status':r.status,'plan':r.plan} for r in routes],
        'orders':[{'id':str(o.id),'number':o.number,'status':o.status,'route_id':str(o.route_id) if o.route_id else None,
                   'pricing_status':o.pricing.get('status'),'review_reason':o.pricing.get('review_reason'),
                   'attention_flags':[flag for flag,_ in attention[o.id]]} for o in orders],
        'needs_attention':[{'id':str(i.id),'order_id':str(i.order_id),'kind':i.kind,'description':i.description} for i in issues] +
            [{'id':None,'order_id':str(o.id),'kind':flag,'description':description}
                for o in orders for flag,description in attention[o.id]] +
            [{'id':None,'order_id':str(o.id),'kind':'INVOICE','description':'Invoice needs actual billable time review.'
                if o.pricing.get('method') == 'HOURLY' else 'Invoice needs review.'} for o in invoice_holds]}



@router.get('/analytics/export.csv')
def export_analytics(slug: str, db: DB, date_from: datetime | None = None, date_to: datetime | None = None, user: User = Depends(auth.dispatcher)):
    import csv
    import io
    from fastapi.responses import Response
    data=analytics(slug,db,date_from,date_to,user)
    buffer=io.StringIO()
    writer=csv.writer(buffer)
    writer.writerow(['Order','Scheduled at','Status','Total'])
    for row in data['rows']: writer.writerow([row['number'],row['scheduled_at'],row['status'],row['total'] or ''])
    return Response(buffer.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="analytics.csv"'})
