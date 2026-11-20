"""Driver-scoped road geometry for the authenticated driver's own route."""
from fastapi import HTTPException
from sqlalchemy import select

from . import dispatch, openrouteservice, tomtom, travel
from .common import record
from .models import Order, OrderStop, Route, RouteStop, Vehicle
from .road_profiles import vehicle_data


def require_truck_provider(provider: str) -> str:
    if provider not in {'tomtom', 'openrouteservice'}:
        raise HTTPException(409, 'Truck road routing is not configured.')
    return provider


def path(db, actor, identity):
    route = record(db, Route, actor, identity)
    if route.driver_id != actor.driver_id:
        raise HTTPException(404, 'Route not found.')
    if route.status == 'CANCELLED':
        return {'points': []}

    visits = db.scalars(select(RouteStop).where(
        RouteStop.organization_id == actor.organization_id,
        RouteStop.route_id == route.id,
    ).order_by(RouteStop.position)).all()
    if route.status == 'IN_PROGRESS':
        visits = [visit for visit in visits if visit.status != 'COMPLETED']
    addresses = [record(db, OrderStop, actor, visit.stop_id).data['address'] for visit in visits]
    origin = None
    if route.status in {'PLANNED', 'IN_PROGRESS'}:
        vehicle = record(db, Vehicle, actor, route.vehicle_id)
        origin, _ = dispatch.route_map_origin(db, actor, route, vehicle)
        if origin:
            addresses.insert(0, origin)
    if len(addresses) < 2:
        return {'points': []}
    if any(address.get('latitude') is None or address.get('longitude') is None for address in addresses):
        raise HTTPException(409, 'Route locations are missing map coordinates.')

    provider = require_truck_provider(travel.dispatch_provider())
    vehicle = record(db, Vehicle, actor, route.vehicle_id)
    orders = db.scalars(select(Order).where(
        Order.organization_id == actor.organization_id,
        Order.route_id == route.id,
    )).all()
    hazardous = any(item.get('dangerous_goods') for order in orders for item in order.facts['items'])
    if provider == 'tomtom':
        result = tomtom.route(addresses, vehicle_data(db, actor.organization_id, vehicle),
            hazardous=hazardous, geometry=True)
        return {'points': result['points'], 'provider': 'TOMTOM', 'truck_profile_applied': True,
            'attribution': '© TomTom'}
    result = openrouteservice.route(addresses, vehicle_data(db, actor.organization_id, vehicle),
        hazardous=hazardous, geometry=True, cache_scope=str(actor.organization_id))
    return {'points': result['points'], 'provider': 'OPENROUTESERVICE', 'truck_profile_applied': True,
        'attribution': openrouteservice.ATTRIBUTION}
