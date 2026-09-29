from datetime import datetime
from uuid import UUID
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, delete
from app.models import User, now
from app.services import audit
from .models import Order, Driver, Vehicle, Route, RouteStop, OrderStop, DutySession
from .common import record, company_lock, version, changed, command, settings, dump
from .routing import optimize


def route_view(db, actor, row):
    stops = db.scalars(select(RouteStop).where(RouteStop.organization_id == actor.organization_id, RouteStop.route_id == row.id).order_by(RouteStop.position)).all()
    result = jsonable_encoder(dump(row))
    result['stops'] = [{**jsonable_encoder(dump(s)), 'stop': record(db, OrderStop, actor, s.stop_id).data} for s in stops]
    if actor.role == 'DRIVER':
        # Explicit projection: no Order pricing, payer, private notes or other drivers.
        vehicle = record(db, Vehicle, actor, row.vehicle_id)
        orders = db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == row.id)).all()
        result['items'] = [item for order in orders for item in order.facts['items']]
        result['vehicle'] = {k: vehicle.data[k] for k in ['name','plate','payload_kg','volume_m3','length_cm','width_cm','height_cm','equipment']}
    return result


def validate_candidate(db, actor, orders, driver, vehicle, planned_at):
    if vehicle.data.get('availability') == 'UNAVAILABLE': raise HTTPException(409, 'Vehicle is unavailable.')
    if not driver.active or not vehicle.active: raise HTTPException(409, 'Driver and vehicle must be active.')
    account = db.scalar(select(User).where(User.organization_id == actor.organization_id, User.driver_id == driver.id, User.active.is_(True)))
    if not account: raise HTTPException(409, 'Driver account is inactive.')
    if driver.vehicle_id != vehicle.id: raise HTTPException(409, 'Vehicle is not attached to this driver.')
    duty = db.scalar(select(DutySession).where(DutySession.organization_id == actor.organization_id, DutySession.driver_id == driver.id, DutySession.ended_at.is_(None)))
    if not duty: raise HTTPException(409, 'Driver must be On Duty before assignment.')
    _, config = settings(db, actor)
    if len(orders) > (driver.data.get('maximum_active_orders') or config.maximum_active_orders):
        raise HTTPException(409, 'Driver maximum active Orders exceeded.')
    start = driver.data.get('shift_start'); end = driver.data.get('shift_end')
    if start and planned_at < datetime.fromisoformat(start): raise HTTPException(409, 'Route begins before the driver shift.')
    for order in orders:
        if order.pricing.get('status') != 'PRICED': raise HTTPException(409, 'Order needs pricing review before assignment.')
        if order.status == 'NEW' and datetime.fromisoformat(order.pricing['expires_at']) < now(): raise HTTPException(409, 'Order quote has expired; reprice it before assignment.')
        if order.facts.get('vehicle_type_id') and order.facts['vehicle_type_id'] != str(vehicle.type_id):
            raise HTTPException(409, 'Vehicle type does not match the booking.')
        for stop in order.facts['stops']:
            if stop['kind'] == 'PICKUP' and ' '.join(stop['address']['city'].casefold().split()) != driver.service_city:
                raise HTTPException(409, 'Pickup city is outside the driver service area.')
            if stop['kind'] == 'PICKUP' and stop['address']['province'].upper() != driver.address['province'].upper():
                raise HTTPException(409, 'Pickup province is outside the driver service area.')
        if any(i['dangerous_goods'] for i in order.facts['items']) and 'DG' not in driver.data['qualifications']:
            raise HTTPException(409, 'Driver requires a verified DG qualification.')
        context = order.pricing['context']
        if context['service'].get('exclusive_vehicle') and len(orders) > 1:
            raise HTTPException(409, 'Exclusive service cannot share a Route.')
        for accessory in context['accessorials']:
            if not set(accessory.get('required_equipment',[])).issubset(vehicle.data['equipment']): raise HTTPException(409, 'Vehicle lacks required equipment.')
            if accessory.get('required_crew',1) > driver.data['crew_size']: raise HTTPException(409, 'Required crew is unavailable.')
    return datetime.fromisoformat(end) if end else None


def rebuild(db, actor, route, orders, driver, vehicle, planned_at):
    end = validate_candidate(db, actor, orders, driver, vehicle, planned_at)
    stops = []
    for order in orders:
        for original in order.facts['stops']:
            stop = dict(original)
            if stop['kind'] == 'PICKUP':
                requested = datetime.fromisoformat(stop['window_start']) if stop.get('window_start') else order.scheduled_at
                stop['window_start'] = max(requested, order.scheduled_at).isoformat()
            stops.append(stop)
    items = [i for order in orders for i in order.facts['items']]
    from .travel import matrix
    travel_table = matrix(db,actor,stops)
    plan = optimize(stops, items, vehicle.data, planned_at, end, driver.data['maximum_work_minutes'], travel_table)
    existing = {str(s.stop_id): s for s in db.scalars(select(RouteStop).where(RouteStop.route_id == route.id, RouteStop.organization_id == actor.organization_id))}
    # Move positions temporarily to avoid unique collisions while preserving stable visit IDs.
    for stop in existing.values(): stop.position += 10000
    db.flush()
    ids = {s['stop_id'] for s in plan['stops']}
    for key, stop in existing.items():
        if key not in ids: db.delete(stop)
    for index, item in enumerate(plan['stops']):
        row = existing.get(item['stop_id'])
        if row is None:
            row = RouteStop(organization_id=actor.organization_id, route_id=route.id, stop_id=UUID(item['stop_id']), status='PENDING')
            db.add(row)
        row.position, row.planned_at = index, datetime.fromisoformat(item['planned_at'])
    route.plan, route.planned_at = plan, planned_at
    db.flush()


def assign(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        order = record(db, Order, actor, identity, True); version(order, data.version)
        if order.status != 'NEW': raise HTTPException(409, 'Only unassigned Orders can be assigned.')
        driver = record(db, Driver, actor, data.driver_id, True)
        vehicle = record(db, Vehicle, actor, data.vehicle_id, True)
        if data.route_id:
            route = record(db, Route, actor, data.route_id, True)
            if data.route_version is None: raise HTTPException(422, 'Existing Route version is required.')
            version(route, data.route_version)
            if route.status != 'PLANNED' or route.locked: raise HTTPException(409, 'Executing or locked Routes cannot change.')
            if route.driver_id != driver.id or route.vehicle_id != vehicle.id: raise HTTPException(409, 'Route custody does not match the selection.')
        else:
            route = Route(organization_id=actor.organization_id, driver_id=driver.id, vehicle_id=vehicle.id,
                status='PLANNED', planned_at=data.planned_at, plan={})
            db.add(route); db.flush()
        orders = list(db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == route.id))) + [order]
        rebuild(db, actor, route, orders, driver, vehicle, data.planned_at)
        order.route_id, order.status = route.id, 'ASSIGNED'
        changed(db, actor, order, 'order.assigned')
        if data.route_id: changed(db, actor, route, 'route.optimized')
        else: audit(db, actor, 'route.created', route.id, actor.organization_id)
        return {'order_id': str(order.id), 'order_version': order.version, 'route': route_view(db, actor, route)}
    return command(db, actor, key, 'assign:' + str(identity), data.model_dump(mode='json'), run)


def release(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        route = record(db, Route, actor, identity, True); version(route, data.version)
        if route.generation != data.generation: raise HTTPException(409, 'Assignment generation changed.')
        if route.status != 'PLANNED': raise HTTPException(409, 'Only a planned Route can be released.')
        for order in db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == identity).with_for_update()):
            order.route_id, order.status = None, 'NEW'; changed(db, actor, order, 'order.unassigned')
        db.execute(delete(RouteStop).where(RouteStop.organization_id == actor.organization_id, RouteStop.route_id == identity))
        route.status, route.locked = 'CANCELLED', False
        route.generation += 1
        changed(db, actor, route, 'route.released')
        return route_view(db, actor, route)
    return command(db, actor, key, 'release:' + str(identity), data.model_dump(), run)
