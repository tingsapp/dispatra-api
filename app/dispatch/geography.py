"""Dispatch geography is independent of commercial pricing zones."""
import math
from app.operations.calendar import day_bounds, opening
from app.operations.pricing import matching_zones
from app.operations.schemas import RateData


def km(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(math.radians(b['longitude']-a['longitude'])/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def heading(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    delta = math.radians(b['longitude']-a['longitude'])
    return round(math.degrees(math.atan2(math.sin(delta)*math.cos(lat2),
        math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(delta)))) % 360


def movement(points, target):
    if len(points) < 2: return 'UNKNOWN', None
    current = points[0]
    previous = next((p for p in points[1:] if (current.captured_at-p.captured_at).total_seconds() >= 15), None)
    if previous is None: return 'UNKNOWN', None
    moved = km(previous.data, current.data)*1000
    uncertainty = max(float(previous.data['accuracy_m']), float(current.data['accuracy_m']))
    if moved <= max(25, uncertainty): return 'STATIONARY', None
    bearing = heading(previous.data, current.data)
    towards = heading(current.data, target)
    angle = abs((bearing-towards+180) % 360-180)
    return ('APPROACHING' if angle < 60 else 'MOVING_AWAY' if angle > 120 else 'CROSSING'), bearing


def zone(address):
    if address.get('latitude') is None or address.get('longitude') is None: return None
    # Stable five-kilometre cells are soft grouping hints; route costs resolve border cases.
    lat, lon = address['latitude'], address['longitude']
    return f'Area {math.floor(lat*111/5)}:{math.floor(lon*111*math.cos(math.radians(lat))/5)}'


def zones(order):
    return {value for stop in order.facts['stops'] if (value := zone(stop['address'])) is not None}


def delivery_zone(order):
    return next((zone(s['address']) for s in order.facts['stops'] if s['kind']=='DROPOFF'), None)


class HomeZone:
    """The Zone Rate Card zone of an Order's first drop-off; vehicles whose parking address lies in it are home vehicles."""

    def __init__(self, card, zone_id):
        self.card, self.zone_id = card, zone_id
        self.name = next(z.name for z in card.zones if z.id == zone_id)

    def contains(self, address):
        return matching_zones(self.card, (address or {}).get('postal_code')) == {self.zone_id}


def morning_zone(order, config, at):
    """The HomeZone whose vehicles take this Order first, or None when the rule does not apply.

    The morning pass is today's pickups (or overdue ones) booked before today's opening time and priced with a
    Zone Rate Card; Orders booked after opening or priced by distance, fixed amount or hour are dispatched by
    location, capacity and workload alone. The Order's frozen pricing card decides the zone.
    """
    if order.created_at >= opening(config, at) or order.scheduled_at >= day_bounds(config, at)[1]: return None
    snapshot = ((order.pricing or {}).get('context') or {}).get('card')
    if not snapshot or snapshot.get('method') != 'ZONE': return None
    card = RateData.model_validate(snapshot)
    drop = next((s['address'] for s in order.facts['stops'] if s['kind'] == 'DROPOFF'), {})
    ids = matching_zones(card, drop.get('postal_code'))
    return HomeZone(card, ids.pop()) if len(ids) == 1 else None
