"""Deterministic step: which drivers can take an Order, and rule metrics that rank them without AI.

Each candidate passes the same checks `assign` applies (custody, duty, service area, equipment, capacity, windows,
shift and work time) using the built-in travel estimator, so screening never calls a paid provider. The assignment
command re-checks everything with road travel before it saves.
"""
import math
from datetime import datetime, timedelta
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from app.operations.dispatch import mergeable_route, plan_route
from app.operations.models import Driver, DutySession, Location, Order, Vehicle

KPH = 35  # same speed as the routing estimator
FRESH_GPS = timedelta(minutes=30)
UNKNOWN_POSITION_MINUTES = 30


def km(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b['longitude'] - a['longitude']) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def position(db, actor, driver):
    """Latest GPS from the open duty session when fresh, else the driver's home address coordinates."""
    point = db.scalar(select(Location).join(DutySession, DutySession.id == Location.duty_id).where(Location.organization_id == actor.organization_id,
        Location.driver_id == driver.id, DutySession.ended_at.is_(None)).order_by(Location.captured_at.desc(), Location.id.desc()).limit(1))
    if point and now() - point.captured_at <= FRESH_GPS: return point.data, 'GPS'
    home = driver.address or {}
    if home.get('latitude') is not None and home.get('longitude') is not None: return home, 'HOME'
    return None, None


def blocker(order):
    """An Order-level reason no driver can be chosen, or None."""
    if order.status != 'NEW': return 'Only unassigned Orders can be dispatched.'
    if order.pricing.get('status') != 'PRICED': return 'Order needs pricing review before assignment.'
    if datetime.fromisoformat(order.pricing['expires_at']) < now(): return 'Order quote has expired; reprice it before assignment.'
    return None


def _measure(db, actor, order, driver, vehicle, route, on_route, planned_at, plan):
    mine = {stop['id']: stop for stop in order.facts['stops']}
    visits = [visit for visit in plan['stops'] if visit['stop_id'] in mine]
    slack = [(datetime.fromisoformat(mine[v['stop_id']]['window_end']) - datetime.fromisoformat(v['planned_at'])).total_seconds() / 60
        for v in visits if mine[v['stop_id']]['kind'] == 'DROPOFF' and mine[v['stop_id']].get('window_end')]
    here, source = position(db, actor, driver)
    first = mine[visits[0]['stop_id']]['address']
    deadhead = round(km(here, first), 1) if here else None
    if route:
        extra = plan['travel_minutes'] - (route.plan or {}).get('travel_minutes', 0)
    else:
        extra = plan['travel_minutes'] + (deadhead / KPH * 60 if deadhead is not None else UNKNOWN_POSITION_MINUTES)
    preferred = str(driver.id) == str(order.facts.get('preferred_driver_id') or '')
    requested = order.facts.get('vehicle_type_id')
    exact = not requested or str(vehicle.type_id) == str(requested)
    facts = ['Shipper requested this driver'] if preferred else []
    if route: facts.append(f'Joins the planned route with {len(on_route)} order{"s" if len(on_route) != 1 else ""}, adding about {round(max(extra, 0))} min of driving')
    else: facts.append(f'Starts a new route, about {round(extra)} min of driving including the trip to pickup')
    if deadhead is not None: facts.append(f'About {deadhead} km from the first pickup ({"live GPS" if source == "GPS" else "home address"})')
    else: facts.append('Current position unknown')
    if slack: facts.append(f'Planned to deliver {round(min(slack))} min before the earliest deadline')
    if not exact: facts.append(f'Different vehicle type than booked ({(order.pricing.get("context") or {}).get("vehicle", {}).get("name") or "requested type"})')
    metrics = {'extra_minutes': round(extra, 1), 'deadhead_km': deadhead, 'position': source, 'route_orders': len(on_route),
        'slack_minutes': round(min(slack)) if slack else None, 'preferred': preferred, 'exact_vehicle_type': exact}
    score = max(extra, 0) + 10 * len(on_route) + (0 if exact else 15) - (20 if preferred else 0)
    return {'driver_id': str(driver.id), 'driver_name': driver.name, 'driver_number': driver.number,
        'vehicle_id': str(vehicle.id), 'vehicle_name': vehicle.data.get('name') or vehicle.number,
        'route_id': str(route.id) if route else None, 'planned_at': planned_at.isoformat(), 'first_arrival': visits[0]['planned_at'],
        'metrics': metrics, 'facts': facts, 'score': round(score, 1), 'reason': ''}


def evaluate(db, actor, order):
    """Return (candidates, excluded). Candidates are feasible and sorted by rule score; excluded carry the failed check."""
    drivers = db.scalars(select(Driver).where(Driver.organization_id == actor.organization_id, Driver.active.is_(True),
        Driver.archived_at.is_(None)).order_by(Driver.number)).all()
    start = max(order.scheduled_at, now())
    candidates, excluded = [], []
    for driver in drivers:
        base = {'driver_id': str(driver.id), 'driver_name': driver.name, 'driver_number': driver.number}
        vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == actor.organization_id, Vehicle.id == driver.vehicle_id)) if driver.vehicle_id else None
        if vehicle is None:
            excluded.append({**base, 'reason': 'No vehicle is attached to this driver.'})
            continue
        try:
            route = mergeable_route(db, actor, driver, vehicle)
            on_route = list(db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == route.id))) if route else []
            planned_at = route.planned_at if route else start
            plan = plan_route(db, actor, on_route + [order], driver, vehicle, planned_at, road=False)
        except HTTPException as error:
            excluded.append({**base, 'reason': str(error.detail)})
            continue
        candidates.append(_measure(db, actor, order, driver, vehicle, route, on_route, planned_at, plan))
    candidates.sort(key=lambda c: (c['score'], c['driver_number']))
    return candidates, excluded
