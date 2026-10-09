from collections import Counter, defaultdict
from datetime import datetime, timedelta
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_, func
from app import auth
from app.models import User, now
from app.routes import DB
from app.dispatch.service import no_driver
from .models import Order, Shipper, Driver, Vehicle, Catalog, Route, Location, DutySession, Issue, RouteStop, OrderStop, Evidence
from .schemas import AnalyticsView, DriverActivityView, MonitorView
from .common import record
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


def verified_proof(stop, visit, evidence, driver_id):
    """Count only the evidence actually accepted by a driver completion."""
    movement = visit.movements or {}
    if movement.get('completed_by') == 'DISPATCHER': return False
    accepted = set(movement.get('evidence_ids') or [])
    kinds = {e.kind for e in evidence if str(e.id) in accepted and e.driver_id == driver_id}
    if movement.get('unattended'):
        valid = stop.data.get('unattended_allowed') and movement.get('safe_placement') and 'PHOTO' in kinds
    else:
        valid = bool(movement.get('recipient_name')) and 'SIGNATURE' in kinds
    return bool(valid and (not stop.data.get('photo_required') or 'PHOTO' in kinds))


@router.get('/analytics', response_model=AnalyticsView)
def analytics(slug: str, db: DB, date_from: datetime | None = None, date_to: datetime | None = None, user: User = Depends(auth.dispatcher)):
    from zoneinfo import ZoneInfo
    from .common import settings
    orders = dated_orders(db, user, date_from, date_to)
    _, config = settings(db, user)
    zone = ZoneInfo(config.time_zone)
    ids = [o.id for o in orders]
    statuses = Counter(o.status for o in orders)
    issues = Counter(db.scalars(select(Issue.order_id).where(Issue.organization_id == user.organization_id,
        Issue.order_id.in_(ids), Issue.resolved.is_(False)))) if ids else Counter()
    shipper_ids, service_ids = {o.shipper_id for o in orders}, {o.service_id for o in orders}
    route_ids = {o.route_id for o in orders if o.route_id}
    shippers = {r.id: r for r in db.scalars(select(Shipper).where(Shipper.organization_id == user.organization_id, Shipper.id.in_(shipper_ids)))} if shipper_ids else {}
    services = {r.id: r for r in db.scalars(select(Catalog).where(Catalog.organization_id == user.organization_id, Catalog.id.in_(service_ids)))} if service_ids else {}
    routes = {r.id: r for r in db.scalars(select(Route).where(Route.organization_id == user.organization_id, Route.id.in_(route_ids)))} if route_ids else {}
    drivers = {r.id: r for r in db.scalars(select(Driver).where(Driver.organization_id == user.organization_id, Driver.id.in_({r.driver_id for r in routes.values()})))} if routes else {}
    vehicles = {r.id: r for r in db.scalars(select(Vehicle).where(Vehicle.organization_id == user.organization_id, Vehicle.id.in_({r.vehicle_id for r in routes.values()})))} if routes else {}
    finished_ids = [o.id for o in orders if o.status == 'COMPLETED']
    visits = db.execute(select(RouteStop, OrderStop).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(
        RouteStop.organization_id == user.organization_id, OrderStop.order_id.in_(finished_ids),
        OrderStop.kind == 'DROPOFF', RouteStop.status == 'COMPLETED')).all() if finished_ids else []
    observed = {str(stop.id): (visit, stop) for visit, stop in visits}
    evidence = defaultdict(list)
    if observed:
        for row in db.execute(select(Evidence.id, Evidence.stop_id, Evidence.driver_id, Evidence.kind).where(Evidence.organization_id == user.organization_id, Evidence.stop_id.in_([stop.id for _, stop in visits]))):
            evidence[str(row.stop_id)].append(row)
    daily = defaultdict(lambda: {'orders': 0, 'completed': 0, 'on_time': 0, 'late': 0, 'not_measured': 0})
    rows = []
    for order in sorted(orders, key=lambda o: (o.scheduled_at, str(o.id)), reverse=True):
        drops = [s for s in order.facts['stops'] if s['kind'] == 'DROPOFF']
        completed = order.status == 'COMPLETED'
        known = completed and bool(drops) and all(s.get('window_end') and s['id'] in observed and observed[s['id']][0].completed_at and observed[s['id']][0].arrived_at and observed[s['id']][0].movements.get('completed_by') != 'DISPATCHER' for s in drops)
        outcome = ('ON_TIME' if all(observed[s['id']][0].completed_at <= datetime.fromisoformat(s['window_end']) for s in drops) else 'LATE') if known else 'NOT_MEASURED'
        route = routes.get(order.route_id)
        driver = drivers.get(route.driver_id) if route else None
        vehicle = vehicles.get(route.vehicle_id) if route else None
        # Accepted evidence belongs to the published assignment; uploaded-but-unused files do not qualify.
        proof = completed and bool(drops) and all(s['id'] in observed and route and
            verified_proof(observed[s['id']][1], observed[s['id']][0], evidence[s['id']], route.driver_id) for s in drops)
        day = daily[order.scheduled_at.astimezone(zone).date().isoformat()]
        day['orders'] += 1
        if completed:
            day['completed'] += 1
            day[{'ON_TIME': 'on_time', 'LATE': 'late', 'NOT_MEASURED': 'not_measured'}[outcome]] += 1
        rows.append({'id': str(order.id), 'number': order.number, 'scheduled_at': order.scheduled_at.isoformat(),
            'status': order.status, 'source': order.source, 'delivered_at': order.completed_at,
            'shipper_id': str(order.shipper_id), 'driver_id': str(route.driver_id) if route else None,
            'shipper_name': shippers[order.shipper_id].name if order.shipper_id in shippers else '',
            'driver_name': driver.name if driver else '', 'driver_number': driver.number if driver else '',
            'vehicle_unit': vehicle.data.get('unit_number', '') if vehicle else '',
            'service_name': services[order.service_id].data.get('name', '') if order.service_id in services else '',
            'delivery_outcome': outcome, 'pod_verified': bool(proof), 'open_issues': issues[order.id]})
    on_time = sum(r['delivery_outcome'] == 'ON_TIME' for r in rows)
    measured = sum(r['delivery_outcome'] != 'NOT_MEASURED' for r in rows)
    return {'orders': len(orders), 'statuses': dict(statuses), 'completed_orders': statuses['COMPLETED'],
        'open_issues': sum(issues.values()), 'source_counts': dict(Counter(o.source for o in orders)),
        'pod_verified_orders': sum(r['pod_verified'] for r in rows), 'on_time_orders': on_time,
        'sla_known_orders': measured, 'on_time_percent': round(on_time / measured * 100, 1) if measured else None,
        'daily': [{'date': date, **values} for date, values in sorted(daily.items())], 'rows': rows}


@router.get('/drivers/{identity}/activity', response_model=DriverActivityView)
def driver_activity(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    driver = record(db,Driver,user,identity)
    completed = list(db.scalars(select(Order).join(Route,Route.id == Order.route_id).where(Order.organization_id == user.organization_id,
        Route.driver_id == identity,Order.status.in_(['COMPLETED']))))
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
    for order_id, description in no_driver(db, user.organization_id, orders).items(): attention[order_id].append(('NO_DRIVER', description))
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
                for o in orders for flag,description in attention[o.id]]}



@router.get('/analytics/export.csv')
def export_analytics(slug: str, db: DB, date_from: datetime | None = None, date_to: datetime | None = None, user: User = Depends(auth.dispatcher)):
    import csv
    import io
    from fastapi.responses import Response
    data=analytics(slug,db,date_from,date_to,user)
    buffer=io.StringIO()
    writer=csv.writer(buffer)
    writer.writerow(['Order', 'Scheduled at', 'Status', 'Source', 'Shipper', 'Driver', 'Delivered at', 'Delivery outcome', 'POD verified', 'Open issues'])
    for row in data['rows']:
        values = [row[key] for key in ['number', 'scheduled_at', 'status', 'source', 'shipper_name', 'driver_name', 'delivered_at', 'delivery_outcome', 'pod_verified', 'open_issues']]
        writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in values])
    return Response(buffer.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="analytics.csv"'})
