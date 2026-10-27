"""Own-Order map geometry. Authorization precedes the narrow vehicle lookup."""
from . import travel
from .schemas import Booking


def path(db, actor, order):
    booking = Booking.model_validate(order.facts)
    # Unassigned Orders retain the existing commercial road preview. This is not
    # truck feasibility evidence; assigned truck routing still fails closed.
    if not order.route_id or travel.dispatch_provider() != 'tomtom':
        return {'points': travel.order_road_path(db, actor, booking), 'provider': 'GOOGLE',
                'truck_profile_applied': False, 'attribution': '', 'preview': not bool(order.route_id)}
    from .orders import company_scope
    from .common import record
    from .models import Route, Vehicle
    from . import tomtom
    # Caller has already checked the Order visibility. No other Order's stops leave this projection.
    with company_scope(db, actor):
        route = record(db, Route, actor, order.route_id)
        vehicle = record(db, Vehicle, actor, route.vehicle_id)
        from .road_profiles import vehicle_data as routing_vehicle_data
        vehicle_data = routing_vehicle_data(db, actor.organization_id, vehicle)
        positions = {item['stop_id']: i for i, item in enumerate(route.plan.get('stops', []))}
    stops = sorted(booking.stops, key=lambda stop: positions.get(str(stop.id), len(positions)))
    result = tomtom.route([stop.address.model_dump() for stop in stops], vehicle_data,
        hazardous=any(item.dangerous_goods for item in booking.items), geometry=True)
    return {'points': result['points'], 'provider': 'TOMTOM', 'truck_profile_applied': True, 'attribution': '© TomTom'}
