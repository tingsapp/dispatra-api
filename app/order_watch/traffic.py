"""Acquire live road estimates outside business transactions. No simulated production ETA."""
import math
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select
from app.operations.models import Route,RouteStop,OrderStop,DutySession,Location
from app.operations import travel
from .evidence import location_usable,separation


def route_input(db,actor,route,current):
    if route.status!='IN_PROGRESS':return None
    visits=db.scalars(select(RouteStop).where(RouteStop.organization_id==actor.organization_id,RouteStop.route_id==route.id,
        RouteStop.status!='COMPLETED').order_by(RouteStop.position)).all()
    if not visits or visits[0].arrived_at is not None:return None  # Remaining handling duration is unknown.
    duty=db.scalar(select(DutySession).where(DutySession.organization_id==actor.organization_id,DutySession.driver_id==route.driver_id,
        DutySession.ended_at.is_(None)))
    location=db.scalar(select(Location).where(Location.organization_id==actor.organization_id,Location.driver_id==route.driver_id,
        Location.duty_id==duty.id).order_by(Location.captured_at.desc()).limit(1)) if duty else None
    if not location_usable(location,duty,current):return None
    stops={s.id:s for s in db.scalars(select(OrderStop).where(OrderStop.organization_id==actor.organization_id,
        OrderStop.id.in_([v.stop_id for v in visits])))}
    targets=[dict(stop_id=str(v.stop_id),address=stops[v.stop_id].data['address'],
        service_minutes=stops[v.stop_id].data.get('service_minutes')) for v in visits]
    if len(targets)>25 or any(t['address'].get('latitude') is None or t['address'].get('longitude') is None for t in targets):return None
    return dict(route_id=str(route.id),route_version=route.version,generation=route.generation,next_stop_id=str(visits[0].stop_id),
        captured_at=location.captured_at,duty_id=str(duty.id),origin=location.data,targets=targets,
        straight_line_metres=separation(location,targets[0]['address']),accuracy_metres=float(location.data['accuracy_m']))


def seconds(value):
    result=float(value.removesuffix('s'))
    if not math.isfinite(result) or result<0:raise ValueError('Invalid duration')
    return result


def obtain(actor,snapshot,current):
    if travel.provider()!='google':return None
    targets=snapshot['targets']
    result=travel.google(None,actor,'directions/v2:computeRoutes',dict(
        origin=travel.waypoint(snapshot['origin']),destination=travel.waypoint(targets[-1]['address']),
        intermediates=[travel.waypoint(t['address']) for t in targets[:-1]],
        travelMode='DRIVE',routingPreference='TRAFFIC_AWARE'),
        'routes.legs.duration,routes.legs.staticDuration,routes.legs.distanceMeters')
    try:
        legs=result['routes'][0]['legs']
        if len(legs)!=len(targets):raise ValueError('Incomplete legs')
        rows=[];elapsed=added=distance=0
        for target,leg in zip(targets,legs):
            duration=seconds(leg['duration']);baseline=seconds(leg['staticDuration'])
            metres=float(leg['distanceMeters'])
            if not math.isfinite(metres) or metres<0:raise ValueError('Invalid distance')
            elapsed+=duration;added+=max(0,duration-baseline);distance+=metres
            rows.append(dict(stop_id=target['stop_id'],eta=(current+timedelta(seconds=elapsed)).isoformat(),
                traffic_added_seconds=round(added),road_distance_metres=round(distance)))
            service=target['service_minutes']
            # Do not fabricate downstream handling time if absent.
            if service is None:break
            elapsed+=service*60
        return dict(source='Google traffic-aware road estimate',vehicle_access_verified=False,stops=rows,
            straight_line_metres=snapshot.get('straight_line_metres'),accuracy_metres=snapshot.get('accuracy_metres'),duty_id=snapshot.get('duty_id'))
    except (KeyError,IndexError,ValueError,TypeError,AttributeError):
        raise HTTPException(503,'Traffic estimate could not be verified.') from None
