"""Order tracking projection for Shippers and dispatchers: milestones, ETA and, when allowed, the driver's live position.

A Shipper sees the driver's position for the whole Route only when every Order on it is theirs (a dedicated
vehicle). On a shared Route the position appears only while the driver's next visit is one of this Order's stops;
otherwise the Shipper sees how many stops come before theirs. Other customers' stops are never returned.
"""
from datetime import datetime, timedelta
from sqlalchemy import select
from app.models import now
from .models import Catalog, Driver, DutySession, Issue, Location, Order, Route, RouteStop, Vehicle
from .orders import company_scope, visible_order

STALE_AFTER = timedelta(minutes=10)
STOP_LABELS = {'PICKUP': ('Driver arrived at pickup', 'Picked up'), 'DROPOFF': ('Driver arrived at delivery', 'Delivered')}
ISSUE_LABELS = {'FAILED_PICKUP': 'Pickup could not be completed', 'FAILED_DELIVERY': 'Delivery attempt failed', 'SHORT_LOAD': 'Short load reported',
    'WRONG_ADDRESS': 'Address problem reported', 'UNSAFE': 'Unsafe location reported', 'VEHICLE': 'Vehicle issue reported', 'OTHER': 'Issue reported'}


def _stage(order, route, mine, next_visit):
    if order.status == 'CANCELLED': return 'CANCELLED'
    if order.status in {'COMPLETED', 'INVOICED'}: return 'DELIVERED'
    if route is None: return 'BOOKED'
    if route.status == 'PLANNED': return 'ASSIGNED'
    pickups_done = all(v.status == 'COMPLETED' for v, kind in mine if kind == 'PICKUP')
    if not pickups_done: return 'TO_PICKUP'
    return 'OUT_FOR_DELIVERY' if next_visit is not None and any(v.id == next_visit.id for v, _ in mine) else 'IN_TRANSIT'


def order_tracking(db, actor, identity):
    order = visible_order(db, actor, identity)
    stops = {stop['id']: stop for stop in order.facts['stops']}
    issues = db.scalars(select(Issue).where(Issue.organization_id == actor.organization_id, Issue.order_id == order.id).order_by(Issue.created_at)).all()
    route = driver = vehicle_type = location = None
    visits, shared = [], False
    if order.route_id:
        with company_scope(db, actor):
            route = db.scalar(select(Route).where(Route.organization_id == actor.organization_id, Route.id == order.route_id))
            visits = db.scalars(select(RouteStop).where(RouteStop.organization_id == actor.organization_id, RouteStop.route_id == route.id).order_by(RouteStop.position)).all()
            shared = db.scalar(select(Order.id).where(Order.organization_id == actor.organization_id, Order.route_id == route.id,
                Order.shipper_id != order.shipper_id, Order.status != 'CANCELLED').limit(1)) is not None
            row = db.scalar(select(Driver).where(Driver.organization_id == actor.organization_id, Driver.id == route.driver_id))
            driver = (row.name.split() or [''])[0] if row else None
            vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == actor.organization_id, Vehicle.id == route.vehicle_id))
            kind = vehicle and db.scalar(select(Catalog).where(Catalog.organization_id == actor.organization_id, Catalog.id == vehicle.type_id))
            vehicle_type = kind.data.get('name') if kind else None
            on_duty = db.scalar(select(DutySession.id).where(DutySession.organization_id == actor.organization_id,
                DutySession.driver_id == route.driver_id, DutySession.ended_at.is_(None)))
            if route.status == 'IN_PROGRESS' and on_duty:
                location = db.scalar(select(Location).where(Location.organization_id == actor.organization_id, Location.driver_id == route.driver_id,
                    Location.duty_id == on_duty).order_by(Location.captured_at.desc()).limit(1))
    current = now()
    mine = [(v, stops[str(v.stop_id)]['kind']) for v in visits if str(v.stop_id) in stops]
    next_visit = next((v for v in visits if v.status != 'COMPLETED'), None) if route and route.status == 'IN_PROGRESS' else None
    my_next = next((v for v, _ in mine if v.status != 'COMPLETED'), None) if route and route.status in {'PLANNED', 'IN_PROGRESS'} else None
    # Everything after the driver's current visit shifts by however late that visit already is.
    lateness = max(timedelta(0), current - next_visit.planned_at) if next_visit is not None and next_visit.arrived_at is None else timedelta(0)
    eta = lambda visit: max(visit.planned_at + lateness, current) if route and route.status == 'IN_PROGRESS' else visit.planned_at
    by_stop = {str(v.stop_id): v for v, _ in mine}
    stop_rows = []
    for stop in order.facts['stops']:
        visit = by_stop.get(stop['id'])
        open_visit = visit is not None and visit.status != 'COMPLETED' and order.status not in {'CANCELLED', 'COMPLETED', 'INVOICED'}
        stop_rows.append({'id': stop['id'], 'kind': stop['kind'], 'address': stop['address'], 'window_start': stop.get('window_start'), 'window_end': stop.get('window_end'),
            'planned_at': visit.planned_at if visit else None, 'eta': eta(visit) if open_visit else None,
            'arrived_at': visit.arrived_at if visit else None, 'completed_at': visit.completed_at if visit else None,
            'status': visit.status if visit else ('CANCELLED' if order.status == 'CANCELLED' else 'PENDING')})
    target = next((row for row in stop_rows if my_next is not None and row['id'] == str(my_next.stop_id)), None)
    late = bool(target and target['eta'] and target['window_end'] and target['eta'] > datetime.fromisoformat(str(target['window_end']).replace('Z', '+00:00')))
    stops_before = sum(1 for v in visits if my_next is not None and v.position < my_next.position and v.status != 'COMPLETED') if route and route.status == 'IN_PROGRESS' and my_next is not None else None
    leg_is_mine = next_visit is not None and my_next is not None and next_visit.id == my_next.id
    live = route is not None and route.status == 'IN_PROGRESS' and my_next is not None and (actor.role == 'DISPATCHER' or not shared or leg_is_mine)
    fresh = location is not None and current - location.captured_at <= STALE_AFTER
    events = [{'kind': 'BOOKED', 'label': 'Order booked', 'at': order.created_at}]
    if route:
        events.append({'kind': 'ASSIGNED', 'label': f'Driver {driver} assigned' if driver else 'Driver assigned', 'at': route.created_at})
        if route.started_at: events.append({'kind': 'STARTED', 'label': 'Driver started the route', 'at': route.started_at})
    for visit, kind in mine:
        address = stops[str(visit.stop_id)]['address']['text']
        if visit.arrived_at: events.append({'kind': f'{kind}_ARRIVED', 'label': f'{STOP_LABELS[kind][0]} · {address}', 'at': visit.arrived_at})
        if visit.completed_at:
            label = 'Marked delivered by dispatcher' if (visit.movements or {}).get('completed_by') == 'DISPATCHER' else STOP_LABELS[kind][1]
            events.append({'kind': f'{kind}_COMPLETED', 'label': f'{label} · {address}', 'at': visit.completed_at})
    for issue in issues:
        events.append({'kind': 'ISSUE', 'label': ISSUE_LABELS.get(issue.kind, 'Issue reported') + (' (resolved)' if issue.resolved else ''), 'at': issue.created_at})
    if order.status == 'CANCELLED': events.append({'kind': 'CANCELLED', 'label': 'Order cancelled', 'at': order.updated_at})
    events.sort(key=lambda event: event['at'])
    return {'order_id': order.id, 'status': order.status, 'stage': _stage(order, route, mine, next_visit), 'dedicated': route is not None and not shared,
        'driver': {'first_name': driver, 'vehicle_type': vehicle_type} if route and driver else None,
        'stops': stop_rows, 'stops_before_next': stops_before, 'eta': target['eta'] if target else None,
        'delay_minutes': int(lateness.total_seconds() // 60), 'late': late, 'live': live,
        'location': {'latitude': location.data['latitude'], 'longitude': location.data['longitude'], 'accuracy_m': location.data.get('accuracy_m'), 'captured_at': location.captured_at} if live and fresh else None,
        'location_stale': live and not fresh, 'events': events, 'open_issue': any(not issue.resolved for issue in issues), 'updated_at': current}
