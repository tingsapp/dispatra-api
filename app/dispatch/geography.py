"""Dispatch geography is independent of commercial pricing zones."""
import math


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


def zone(address, config):
    postal = address.get('postal_code', '').replace(' ', '').upper()
    for item in config.dispatch_zones:
        if any(postal.startswith(prefix.replace(' ', '').upper()) for prefix in item.postal_codes):
            return item.name
    if address.get('latitude') is None or address.get('longitude') is None: return None
    # Stable five-kilometre cells are soft grouping hints; route costs resolve border cases.
    lat, lon = address['latitude'], address['longitude']
    return f'Area {math.floor(lat*111/5)}:{math.floor(lon*111*math.cos(math.radians(lat))/5)}'


def zones(order, config):
    return {value for stop in order.facts['stops'] if (value := zone(stop['address'], config)) is not None}


def delivery_zone(order, config):
    return next((zone(s['address'], config) for s in order.facts['stops'] if s['kind']=='DROPOFF'), None)
