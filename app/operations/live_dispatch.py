from .road_profiles import vehicle_data
"""Safe planning from confirmed custody. Completed/arrived visits and cargo ownership remain protected."""
from datetime import datetime, timedelta
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from .models import DutySession, Location, RouteStop, Issue
from .routing import optimize

GPS_AGE = timedelta(minutes=2)
GPS_ACCURACY_M = 75


def fixes(db, actor, driver):
    rows = db.scalars(select(Location).join(DutySession, DutySession.id == Location.duty_id).where(
        Location.organization_id == actor.organization_id, Location.driver_id == driver.id,
        DutySession.organization_id == actor.organization_id, DutySession.ended_at.is_(None),
        Location.captured_at >= now() - GPS_AGE, Location.captured_at <= now(),
    ).order_by(Location.captured_at.desc(), Location.id.desc()).limit(8)).all()
    return [r for r in rows if 0 < float(r.data.get('accuracy_m', float('inf'))) <= GPS_ACCURACY_M]


def state(db, actor, route, orders, driver):
    if not route.plan.get('live_updates_supported'):
        raise HTTPException(409, 'Driver must review this route using an updated app before live assignment.')
    if route.plan.get('requires_acknowledgement'):
        raise HTTPException(409, 'Driver must review the latest route before another Order is added.')
    points = fixes(db, actor, driver)
    if not points:
        raise HTTPException(409, 'Live assignment requires accurate driver GPS captured within two minutes.')
    if db.scalar(select(Issue.id).where(Issue.organization_id == actor.organization_id,
            Issue.order_id.in_([o.id for o in orders]), Issue.resolved.is_(False)).limit(1)):
        raise HTTPException(409, 'Resolve the route cargo or delivery issue before adding work.')
    visits = list(db.scalars(select(RouteStop).where(RouteStop.organization_id == actor.organization_id,
        RouteStop.route_id == route.id).order_by(RouteStop.position)))
    completed, pending = [], []
    for visit in visits:
        if visit.status == 'COMPLETED':
            if pending: raise HTTPException(409, 'Route progress is inconsistent; dispatcher review required.')
            completed.append(visit)
        else: pending.append(visit)
    if not pending: raise HTTPException(409, 'Finish the current route before assigning another Order.')
    items = [i for order in orders for i in order.facts['items']]
    load = [Decimal(0)] * 3
    for visit in completed:
        linked = {i['id']: i for i in items if str(visit.stop_id) in (i['pickup_id'], i['delivery_id'])}
        quantities = visit.movements.get('quantities') or {}
        if quantities != {k: i['quantity'] for k, i in linked.items()}:
            raise HTTPException(409, 'Confirmed cargo is incomplete; dispatcher review required.')
        for identity, item in linked.items():
            sign = 1 if item['pickup_id'] == str(visit.stop_id) else -1
            qty = Decimal(quantities[identity]) * sign
            load[0] += qty * Decimal(str(item['weight_kg']))
            load[1] += qty * Decimal(str(item['length_cm'])) * Decimal(str(item['width_cm'])) * Decimal(str(item['height_cm'])) / 1000000
            load[2] += qty * Decimal(item.get('pallets', 0))
    return completed, pending, load, points


def plan(db, actor, route, orders, driver, vehicle, *, road=True):
    from .dispatch import validate_candidate
    # The original start is retained for shift eligibility; remaining work ends at the original work limit.
    shift_end = validate_candidate(db, actor, orders, driver, vehicle, max(route.started_at,route.planned_at))
    completed, pending, load, points = state(db, actor, route, orders, driver)
    done = {str(v.stop_id) for v in completed}
    stops = []
    for order in orders:
        for original in order.facts['stops']:
            if original['id'] in done: continue
            stop = dict(original)
            if stop['kind'] == 'PICKUP':
                requested = datetime.fromisoformat(stop['window_start']) if stop.get('window_start') else order.scheduled_at
                stop['window_start'] = max(requested, order.scheduled_at).isoformat()
            stops.append(stop)
    first = next(s for s in stops if s['id'] == str(pending[0].stop_id))
    start = now()
    from app.order_watch.models import Question, Incident
    readiness = db.scalars(select(Question.estimated_ready_at).join(Incident, Incident.id == Question.incident_id).where(Question.organization_id == actor.organization_id,
        Incident.organization_id == actor.organization_id, Incident.status.in_(('OPEN','ACKNOWLEDGED')),
        (Incident.stop_id.is_(None) | (Incident.stop_id == pending[0].stop_id)),
        Question.route_id == route.id, Question.generation == route.generation, Question.status == 'ANSWERED',
        Question.estimated_ready_at > start)).all()
    if readiness: start = max(readiness)
    from app.dispatch.geography import movement
    _, bearing = movement(points, first['address'])
    origin = {**points[0].data, **({'heading': bearing} if bearing is not None else {})}
    if pending[0].arrived_at:
        # Keep service already underway; its booked arrival window no longer gates a future arrival.
        first.pop('window_end', None)
        first['service_minutes'] = max(0, first.get('service_minutes', 0) - int((now() - pending[0].arrived_at).total_seconds() / 60))
        origin = first['address']
    from .travel import matrix
    table = None
    origins = None
    if road:
        table = matrix(db, actor, [{'id': '__driver__', 'address': origin}] + stops,
            vehicle=vehicle_data(db, actor.organization_id, vehicle), departure=start, hazardous=any(i['dangerous_goods'] for o in orders for i in o.facts['items']))
        if table is not None: origins = {s['id']: table.get(('__driver__', s['id'])) for s in stops}
    deadline = route.started_at + timedelta(minutes=driver.data['maximum_work_minutes'])
    if shift_end: deadline = min(deadline, shift_end)
    if vehicle.data.get('maximum_stops') and len(stops)+len(completed) > vehicle.data['maximum_stops']:
        raise HTTPException(409, 'Route exceeds vehicle Maximum stops.')
    items = [i for o in orders for i in o.facts['items'] if i['delivery_id'] not in done]
    result = optimize(stops, items, vehicle.data, start, deadline, driver.data['maximum_work_minutes'], table,
        initial_load=load, visited=done, first_stop=first['id'] if pending[0].arrived_at else None,
        origin=origin, origin_minutes=origins)
    from .travel import dispatch_provider
    if road and dispatch_provider() == 'tomtom': result['travel_basis'] = 'TOMTOM_TRUCK_TRAFFIC'
    # No new promise may postpone an existing visit beyond its previous feasible promise/window.
    result['planned_at'] = route.planned_at.isoformat()
    result['live_origin_at'] = points[0].captured_at.isoformat()
    return result


def apply(db, actor, route, orders, driver, vehicle):
    from uuid import UUID
    from .common import changed
    result = plan(db, actor, route, orders, driver, vehicle)
    baseline = plan(db, actor, route, [o for o in orders if o.route_id == route.id], driver, vehicle)
    if now()-datetime.fromisoformat(result['live_origin_at']) > GPS_AGE:
        raise HTTPException(409, 'Driver GPS became stale during road planning. Refresh before assigning.')
    remaining = result['travel_minutes']
    result['remaining_travel_minutes'] = remaining
    result['travel_minutes'] = round(max(0, route.plan.get('travel_minutes', baseline['travel_minutes']) + remaining-baseline['travel_minutes']), 2)
    existing = {str(v.stop_id): v for v in db.scalars(select(RouteStop).where(
        RouteStop.organization_id == actor.organization_id, RouteStop.route_id == route.id))}
    done = sorted((v for v in existing.values() if v.status == 'COMPLETED'), key=lambda v: v.position)
    pending = [v for v in existing.values() if v.status != 'COMPLETED']
    for v in pending: v.position += 10000
    db.flush()
    for position, item in enumerate(result['stops'], len(done)):
        visit = existing.get(item['stop_id'])
        if visit is None:
            visit = RouteStop(organization_id=actor.organization_id, route_id=route.id,
                stop_id=UUID(item['stop_id']), status='PENDING')
            db.add(visit)
        visit.position, visit.planned_at = position, datetime.fromisoformat(item['planned_at'])
    prior = route.plan
    result['stops'] = [s for s in prior['stops'] if s['stop_id'] in {str(v.stop_id) for v in done}] + result['stops']
    result['revision'] = int(prior.get('revision', 1)) + 1
    result['requires_acknowledgement'] = True
    result['live_updates_supported'] = True
    route.plan = result
    changed(db, actor, route, 'route.updated')
