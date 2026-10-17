"""Shared feasible candidates, geographic affinity and observed/committed workload."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from app.operations.common import settings
from app.operations.dispatch import mergeable_route, plan_route
from app.operations.live_dispatch import fixes
from app.operations.models import Driver, Order, Vehicle, DutySession
from .geography import km, movement, delivery_zone
from .workload import measure


def position(db, actor, driver):
    points = fixes(db, actor, driver)
    if points: return points[0].data, 'GPS'
    home = driver.address or {}
    if home.get('latitude') is not None and home.get('longitude') is not None: return home, 'HOME'
    return None, None


def blocker(order):
    if order.status != 'NEW': return 'Only unassigned Orders can be dispatched.'
    if order.pricing.get('status') != 'PRICED': return 'Order needs pricing review before assignment.'
    if datetime.fromisoformat(order.pricing['expires_at']) < now(): return 'Order quote has expired; reprice it before assignment.'
    return None


def evaluate(db, actor, order, allocations=None, *, road=True, include_expected=False):
    """The same planner is used for recommendations, fleet proposals and actual assignment checks."""
    _, config = settings(db, actor)
    drivers = db.scalars(select(Driver).where(Driver.organization_id == actor.organization_id,
        Driver.active.is_(True), Driver.archived_at.is_(None)).order_by(Driver.number)).all()
    candidates, excluded = [], []
    for driver in drivers:
        base = {'driver_id': str(driver.id), 'driver_name': driver.name, 'driver_number': driver.number}
        vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == actor.organization_id,
            Vehicle.id == driver.vehicle_id)) if driver.vehicle_id else None
        if vehicle is None:
            excluded.append({**base, 'reason': 'No vehicle is attached to this driver.'}); continue
        try:
            route = mergeable_route(db, actor, driver, vehicle, allow_live=True)
            on_route = list(db.scalars(select(Order).where(Order.organization_id == actor.organization_id,
                Order.route_id == route.id))) if route else []
            additions = (allocations or {}).get(str(driver.id), [])
            on_route += additions
            if route and route.status == 'IN_PROGRESS' and additions:
                raise HTTPException(409, 'Review the first live route update before adding another Order.')
            if route is None and additions:
                route = SimpleNamespace(id=None, status='PLANNED', planned_at=max(now(),min(o.scheduled_at for o in additions)), plan={})
            start = route.planned_at if route else max(order.scheduled_at, now())
            shift_start = datetime.fromisoformat(driver.data['shift_start']) if driver.data.get('shift_start') else None
            if (route is None or route.id is None) and shift_start: start = max(start, shift_start)
            duty = db.scalar(select(DutySession.id).where(DutySession.organization_id==actor.organization_id,
                DutySession.driver_id==driver.id,DutySession.ended_at.is_(None)))
            expected = bool(include_expected and not duty and shift_start and now()<shift_start<=now()+timedelta(hours=1) and (route is None or route.status=='PLANNED'))
            here, source = position(db, actor, driver)
            if here is None: raise HTTPException(409, 'A verified driver starting position is required for planning.')
            pickups = [s for s in order.facts['stops'] if s['kind']=='PICKUP']
            direction, bearing = movement(fixes(db, actor, driver), pickups[0]['address'])
            origin = {**here, **({'heading':bearing} if bearing is not None else {})} if here else None
            live = route is not None and route.status == 'IN_PROGRESS'
            if live:
                from app.operations.live_dispatch import plan as live_plan
                baseline = live_plan(db, actor, route, on_route, driver, vehicle, road=road)
                plan = live_plan(db, actor, route, on_route+[order], driver, vehicle, road=road)
            else:
                baseline = plan_route(db, actor, on_route, driver, vehicle, start, road=road, origin=origin, allow_off_duty=expected) if on_route else None
                plan = plan_route(db, actor, on_route+[order], driver, vehicle, start, road=road, origin=origin, allow_off_duty=expected)
            mine = {s['id']:s for s in order.facts['stops']}
            visits = [v for v in plan['stops'] if v['stop_id'] in mine]
            slack = [(datetime.fromisoformat(mine[v['stop_id']]['window_end'])-datetime.fromisoformat(v['planned_at'])).total_seconds()/60
                for v in visits if mine[v['stop_id']]['kind']=='DROPOFF' and mine[v['stop_id']].get('window_end')]
            extra = max(0, plan['travel_minutes']-(baseline['travel_minutes'] if baseline else 0))
            worked, remaining, completed, utilization = measure(db, actor, driver, config, route, plan, on_route+[order])
            area = delivery_zone(order, config)
            same_area = bool(on_route and area in {delivery_zone(o,config) for o in on_route})
            preferred = str(driver.id) == str(order.facts.get('preferred_driver_id') or '')
            exact = not order.facts.get('vehicle_type_id') or str(vehicle.type_id)==str(order.facts['vehicle_type_id'])
            deadhead = round(km(here,pickups[0]['address']),1) if here else None
            active = sum(o.status not in {'COMPLETED','CANCELLED'} for o in on_route)
            score = extra + (8 if on_route and not same_area else 0) + min(utilization*15,30) + (completed+active)*0.5 + (0 if exact else 2) - (1 if preferred else 0)
            facts = []
            if expected: facts.append(f'Tentative: driver expected On Duty at {shift_start.isoformat()}; no assignment before duty confirmation')
            if (order.booking or {}).get('assigned_driver'): facts.append('Shipper’s assigned driver')
            elif preferred: facts.append('Shipper requested this driver')
            if live: facts.append(f'Joins the remaining route, adding about {round(extra)} min of driving; driver review required')
            elif route: facts.append(f'Joins the planned route with {active} orders, adding about {round(extra)} min of driving')
            else: facts.append(f'Starts a new route, about {round(extra)} min of driving including the trip to pickup')
            if area:
                label = area if any(z.name == area for z in config.dispatch_zones) else 'Nearby delivery area'
                facts.append(label + (' · compatible delivery area' if same_area else ' · delivery area'))
            facts.append(f'{round(worked)} min worked today; about {round(remaining)} min committed; {completed} orders completed')
            if live: facts.append({'APPROACHING':'Approaching the new pickup', 'MOVING_AWAY':'Moving away; remaining-route detour considered',
                'CROSSING':'Travelling across the pickup direction', 'STATIONARY':'Stationary; planned route used', 'UNKNOWN':'Movement direction unavailable; planned route used'}[direction])
            elif source == 'HOME': facts.append('Starting position estimated from driver address, not live GPS')
            if slack: facts.append(f'Planned delivery slack: {round(min(slack))} min')
            candidates.append({**base, 'vehicle_id':str(vehicle.id), 'vehicle_name':vehicle.data.get('name') or vehicle.number,
                'route_id':str(route.id) if route and route.id else None, 'route_version':getattr(route,'version',None),
                'planned_at':start.isoformat(), 'first_arrival':visits[0]['planned_at'], 'score':round(score,2), 'reason':'', 'facts':facts,
                'metrics':{'extra_minutes':round(extra,1), 'deadhead_km':deadhead, 'position':source,
                    'route_orders':active, 'slack_minutes':round(min(slack)) if slack else None, 'preferred':preferred,
                    'exact_vehicle_type':exact, 'zone':area, 'zone_match':same_area, 'direction':direction,
                    'worked_minutes':worked, 'remaining_minutes':remaining, 'completed_orders':completed,
                    'workload_ratio':utilization, 'live_insertion':live, 'road_verified':plan['is_road_eta'], 'awaiting_duty':expected}})
        except HTTPException as error:
            excluded.append({**base,'reason':str(error.detail)})
    candidates.sort(key=lambda c:(c['score'],c['driver_number']))
    return candidates, excluded
