"""The driver picks which pending visit comes next, any of them. The other pending visits keep their relative order.

A drop-off whose goods are not on board brings its pending pickups along, just ahead of it. An arrival-confirmed
current visit is locked and the vehicle never overloads. Late delivery windows are warnings the driver must accept.
ETAs are re-estimated for the new order.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from .models import Driver, RouteStop, Order, Vehicle
from .common import record, changed, command, company_lock
from .dispatch import route_view
from .execution import own_route, route_revision
from .live_dispatch import confirmed_load, fixes
from .routing import travel_minutes

MOVES_KEPT = 20


def _road_minutes(db, actor, route, origin, stops, hazardous):
    from .travel import matrix
    from .road_profiles import vehicle_data
    places = ([{'id': '__driver__', 'address': origin}] if origin else []) + stops
    try:
        vehicle = record(db, Vehicle, actor, route.vehicle_id)
        return matrix(db, actor, places, vehicle=vehicle_data(db, actor.organization_id, vehicle), departure=now(), hazardous=hazardous)
    except HTTPException:
        # Road routing unavailable: the straight-line estimate still orders the ETAs sensibly.
        return None


def proposal(db, actor, route, visit_id):
    """The pending visits with `visit_id` first, their estimated arrivals and the windows they would miss."""
    if route.status != 'IN_PROGRESS': raise HTTPException(409, 'Route is not executing.')
    visits = list(db.scalars(select(RouteStop).where(RouteStop.organization_id == actor.organization_id,
        RouteStop.route_id == route.id).order_by(RouteStop.position)))
    completed = [v for v in visits if v.status == 'COMPLETED']
    pending = [v for v in visits if v.status != 'COMPLETED']
    chosen = next((v for v in pending if v.id == visit_id), None)
    if chosen is None:
        if any(v.id == visit_id for v in visits): raise HTTPException(409, 'This visit is already completed.')
        raise HTTPException(404, 'Visit not found.')
    if pending[0].arrived_at: raise HTTPException(409, 'Finish the visit you arrived at first.')
    orders = list(db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == route.id)))
    stops = {s['id']: s for o in orders for s in o.facts['stops']}
    items = [i for o in orders for i in o.facts['items']]
    # The chosen drop-off's goods still to collect: their pickups come first, in their current order.
    needed = {i['pickup_id'] for i in items if i['delivery_id'] == str(chosen.stop_id)}
    first = [v for v in pending if str(v.stop_id) in needed] + [chosen]
    sequence = first + [v for v in pending if v not in first]
    if sequence == pending: raise HTTPException(409, 'This visit is already next.')
    vehicle = record(db, Vehicle, actor, route.vehicle_id).data
    capacities = (Decimal(str(vehicle['payload_kg'])), Decimal(str(vehicle['volume_m3'])), Decimal(vehicle.get('pallet_capacity', 0)))
    load = confirmed_load(completed, items)
    loaded = {str(v.stop_id) for v in completed}
    points = fixes(db, actor, record(db, Driver, actor, route.driver_id))
    last = stops.get(str(completed[-1].stop_id)) if completed else None
    origin = points[0].data if points else last['address'] if last else None
    table = _road_minutes(db, actor, route, origin, [stops[str(v.stop_id)] for v in sequence], any(i.get('dangerous_goods') for i in items))
    clock, previous, schedule = now(), '__driver__' if origin else None, []
    for visit in sequence:
        stop = stops[str(visit.stop_id)]
        if any(i['delivery_id'] == stop['id'] and i['pickup_id'] not in loaded for i in items):
            raise HTTPException(409, 'Pick up the goods for this delivery first.')
        loaded.add(stop['id'])
        for item in items:
            sign = 1 if item['pickup_id'] == stop['id'] else -1 if item['delivery_id'] == stop['id'] else 0
            if not sign: continue
            q = Decimal(item['quantity']) * sign
            load[0] += q * Decimal(str(item['weight_kg']))
            load[1] += q * Decimal(str(item['length_cm'])) * Decimal(str(item['width_cm'])) * Decimal(str(item['height_cm'])) / 1000000
            load[2] += q * Decimal(item.get('pallets', 0))
        if any(value > capacity for value, capacity in zip(load, capacities)):
            raise HTTPException(409, 'The vehicle would be over capacity. Deliver first.')
        if previous:
            minutes = table.get((previous, stop['id'])) if table else None
            here = origin if previous == '__driver__' else stops[previous]['address']
            clock += timedelta(minutes=minutes if minutes is not None else travel_minutes(here, stop['address']))
        if stop.get('window_start'): clock = max(clock, datetime.fromisoformat(stop['window_start']))
        late = bool(stop.get('window_end')) and clock > datetime.fromisoformat(stop['window_end'])
        schedule.append({'visit': visit, 'stop_id': stop['id'], 'planned_at': clock, 'window_end': stop.get('window_end'), 'late': late,
            'load_kg': str(load[0]), 'volume_m3': str(load[1]), 'pallets': str(load[2])})
        clock += timedelta(minutes=stop.get('service_minutes', 0))
        previous = stop['id']
    basis = 'ROAD' if table else 'STRAIGHT_LINE'
    return completed, schedule, basis


def preview(db, actor, identity, visit_id):
    route = own_route(db, actor, identity)
    _, schedule, basis = proposal(db, actor, route, visit_id)
    return {'travel_basis': basis, 'visits': [{'id': row['visit'].id, 'stop_id': row['stop_id'], 'planned_at': row['planned_at'],
        'window_end': row['window_end'], 'late': row['late']} for row in schedule]}


def choose(db, actor, identity, visit_id, data, key):
    def run():
        company_lock(db, actor)
        route = own_route(db, actor, identity, True); route_revision(route, data)
        completed, schedule, basis = proposal(db, actor, route, visit_id)
        if any(row['late'] for row in schedule) and not data.accept_late:
            raise HTTPException(409, 'A stop would miss its time window. Confirm to continue.')
        # The visit that was next before this change.
        before = str(min((row['visit'] for row in schedule), key=lambda v: v.position).stop_id)
        chosen = next(row['stop_id'] for row in schedule if row['visit'].id == visit_id)
        for row in schedule: row['visit'].position += 10000
        db.flush()
        for position, row in enumerate(schedule, len(completed)):
            row['visit'].position, row['visit'].planned_at = position, row['planned_at']
        done = {str(v.stop_id) for v in completed}
        prior = route.plan
        moves = [*prior.get('driver_moves', []), {'stop_id': chosen, 'before_stop_id': before, 'at': now().isoformat()}]
        route.plan = {**prior, 'revision': int(prior.get('revision', 1)) + 1, 'resequenced_basis': basis, 'driver_moves': moves[-MOVES_KEPT:],
            'stops': [s for s in prior['stops'] if s['stop_id'] in done] + [{'stop_id': row['stop_id'], 'planned_at': row['planned_at'].isoformat(),
                'load_kg': row['load_kg'], 'volume_m3': row['volume_m3'], 'pallets': row['pallets']} for row in schedule]}
        changed(db, actor, route, 'route.resequenced')
        return route_view(db, actor, route)
    return command(db, actor, key, 'next-stop:' + str(identity), data.model_dump(mode='json') | {'visit_id': str(visit_id)}, run)
