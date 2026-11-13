"""Dispatcher-only road segment from the assigned vehicle to the next route visit."""
from fastapi import HTTPException
from sqlalchemy import select

from . import travel, tomtom, openrouteservice
from .common import record
from .models import Route, RouteStop, OrderStop, Order, Driver, Vehicle
from .road_profiles import vehicle_data
from .truck_profile import point


def path(db, actor, identity):
    # Keep fleet locations out of the shared Shipper Order road-path projection.
    if actor.role != 'DISPATCHER':
        raise HTTPException(403, 'Dispatcher access required.')
    route = record(db, Route, actor, identity)
    if route.status not in {'PLANNED', 'IN_PROGRESS'}:
        return {'points': []}
    visit = db.scalar(select(RouteStop).where(
        RouteStop.organization_id == actor.organization_id, RouteStop.route_id == route.id,
        RouteStop.status != 'COMPLETED').order_by(RouteStop.position).limit(1))
    if visit is None:
        return {'points': []}
    driver = record(db, Driver, actor, route.driver_id)
    vehicle = record(db, Vehicle, actor, route.vehicle_id)
    from app.dispatch.candidates import position
    origin, _ = position(db, actor, driver, vehicle)
    if origin is None:
        raise HTTPException(409, 'No vehicle location. Set a parking address or get fresh driver GPS.')
    stop = record(db, OrderStop, actor, visit.stop_id)
    addresses = [origin, stop.data['address']]
    if point(addresses[0]) == point(addresses[1]):
        return {'points': []}
    provider = travel.dispatch_provider()
    if provider in {'tomtom', 'openrouteservice'}:
        router = openrouteservice if provider == 'openrouteservice' else tomtom
        orders = db.scalars(select(Order).where(Order.organization_id == actor.organization_id,
            Order.route_id == route.id, Order.status.in_(('ASSIGNED', 'IN_PROGRESS')))).all()
        options = {'cache_scope': str(actor.organization_id)} if provider == 'openrouteservice' else {}
        result = router.route(addresses, vehicle_data(db, actor.organization_id, vehicle),
            hazardous=any(item.get('dangerous_goods') for order in orders for item in order.facts['items']),
            geometry=True, **options)
        return {'points': result['points'], 'provider': provider.upper(), 'truck_profile_applied': True,
                'attribution': openrouteservice.ATTRIBUTION if provider == 'openrouteservice' else '© TomTom'}
    if provider != 'google':
        raise HTTPException(409, 'Dispatch road routing is not configured.')
    result = travel.google(db, actor, 'directions/v2:computeRoutes', {
        'origin': travel.waypoint(origin), 'destination': travel.waypoint(addresses[1]),
        'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_UNAWARE',
    }, 'routes.polyline.encodedPolyline')
    try:
        points = travel.decode_polyline(result['routes'][0]['polyline']['encodedPolyline'])
    except (KeyError, IndexError, TypeError, ValueError):
        raise HTTPException(409, 'No road route was found to the next stop.') from None
    return {'points': points, 'provider': 'GOOGLE'}
