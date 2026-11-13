"""Development HGV routing: one Matrix request ranks stop orders and one vehicle-restricted Directions request confirms the chosen order."""
import json
import hashlib
import os
import urllib.error
import urllib.request
from fastapi import HTTPException
from .truck_profile import number, point, profile

BASE = 'https://api.heigit.org/openrouteservice/v2/directions/driving-hgv/geojson'
MATRIX = 'https://api.heigit.org/openrouteservice/v2/matrix/driving-hgv'
# The Directions waypoint limit, so one restricted route can confirm any planned order.
MAX_LOCATIONS = 50
ATTRIBUTION = '© openrouteservice · © OpenStreetMap contributors'


class NoTruckRoute(Exception):
    pass


def restrictions(vehicle, hazardous=False):
    truck = profile(vehicle, hazardous=hazardous)
    values = dict(length=float(truck.length_m), width=float(truck.width_m), height=float(truck.height_m),
                  weight=float(truck.gross_weight_kg) / 1000)
    if truck.axle_weight_kg is not None:
        values['axleload'] = float(truck.axle_weight_kg) / 1000
    return dict(vehicle_type='hgv', profile_params=dict(restrictions=values))


def request(payload, url=BASE):
    key = os.environ.get('ORS_API_KEY', '').strip()
    if not key:
        raise HTTPException(409, 'openrouteservice is not configured. Add the server API key before assigning orders.')
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={'Authorization': key, 'Content-Type': 'application/json', 'Accept': 'application/geo+json' if url == BASE else 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000: raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict): raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        try:
            code = json.loads(error.read(4096)).get('error', {}).get('code')
        except (ValueError, AttributeError, OSError):
            code = None
        if code == 2009: raise NoTruckRoute() from None
        if error.code in (403, 429):
            raise HTTPException(503, 'openrouteservice rejected the request or its free quota is exhausted. Check the API key and quota in the HeiGIT dashboard, then retry.') from None
        raise HTTPException(503, 'openrouteservice truck routing is unavailable. Retry or request dispatcher review.') from None
    except (OSError, ValueError):
        raise HTTPException(503, 'openrouteservice truck routing is unavailable. Retry or request dispatcher review.') from None


def route(addresses, vehicle, *, departure=None, hazardous=False, geometry=False, cache_scope=None):
    opts = restrictions(vehicle, hazardous)
    if not 2 <= len(addresses) <= 50:
        raise HTTPException(409, 'openrouteservice truck routes support 2–50 locations.')
    points = [point(address) for address in addresses]
    payload = dict(coordinates=[[p['longitude'], p['latitude']] for p in points],
        options=opts, instructions=len(points) > 2)
    heading = addresses[0].get('heading')
    if heading is not None:
        try:
            heading = number(heading)
            if heading >= 360: raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(409, 'Driver heading is invalid.') from None
        payload['bearings'] = [[heading, 45]] + [[] for _ in points[1:]]
    def fetch():
        try:
            result = request(payload)
        except NoTruckRoute:
            raise HTTPException(409, 'No route was found for this truck’s dimensions and weight.') from None
        try:
            feature = result['features'][0]
            properties = feature['properties']
            segments = properties.get('segments')
            if segments is None and len(points) == 2:
                segments = [properties['summary']]
            if len(segments) != len(points) - 1: raise ValueError()
            legs = [dict(seconds=number(row['duration']), metres=number(row['distance'])) for row in segments]
            shape = []
            for xy in feature['geometry']['coordinates']:
                p = point(dict(longitude=xy[0], latitude=xy[1]))
                shape.append([p['latitude'], p['longitude']])
            if len(shape) < 2: raise ValueError()
            return dict(legs=legs, points=shape)
        except (KeyError, IndexError, ValueError, TypeError, HTTPException):
            raise HTTPException(503, 'openrouteservice returned an incomplete truck route.') from None

    if cache_scope is None:
        result = fetch()
    else:
        from .route_cache import directions
        # Exact inputs and tenant/key identity prevent stale facts or cross-tenant reuse.
        signature = json.dumps([str(cache_scope), BASE,
            hashlib.sha256(os.environ.get('ORS_API_KEY', '').encode()).hexdigest(), payload], sort_keys=True)
        key = hashlib.sha256(signature.encode()).hexdigest()
        result = directions.get_or_compute(key, fetch)
    return dict(legs=result['legs'], points=result['points'] if geometry else [])


def matrix(db, stops, vehicle, *, departure=None, hazardous=False, cache_scope=None):
    """Travel minutes between every pair from one Matrix request. The Matrix API cannot apply the truck's
    dimensions and weight, so `confirm` re-checks the chosen order with a restricted route."""
    restrictions(vehicle, hazardous)
    if len(stops) > MAX_LOCATIONS:
        raise HTTPException(409, f'openrouteservice truck routing supports at most {MAX_LOCATIONS} locations including the driver. Split the route.')
    verified = {s['id']: (point(s['address'])['longitude'], point(s['address'])['latitude']) for s in stops}
    places = list(dict.fromkeys(verified.values()))
    durations = [[0]]
    if len(places) > 1:
        payload = dict(locations=[list(p) for p in places], metrics=['duration'])
        def fetch():
            try:
                result = request(payload, MATRIX)
            except NoTruckRoute:
                raise HTTPException(409, 'No route was found for this truck’s dimensions and weight.') from None
            try:
                rows = result['durations']
                if len(rows) != len(places) or any(len(row) != len(places) for row in rows): raise ValueError()
                return [[None if value is None else number(value) for value in row] for row in rows]
            except (KeyError, ValueError, TypeError):
                raise HTTPException(503, 'openrouteservice returned incomplete truck travel times.') from None
        key = hashlib.sha256(json.dumps([str(cache_scope), MATRIX,
            hashlib.sha256(os.environ.get('ORS_API_KEY', '').encode()).hexdigest(), payload], sort_keys=True).encode()).hexdigest()
        cache = db.info.setdefault('ors_matrices', {}) if db is not None else {}
        if key not in cache:
            if cache_scope is None:
                cache[key] = fetch()
            else:
                from .route_cache import directions
                cache[key] = directions.get_or_compute(key, fetch)
        durations = cache[key]
    index = {place: position for position, place in enumerate(places)}
    table = {}
    for origin in stops:
        for destination in stops:
            a, b = index[verified[origin['id']]], index[verified[destination['id']]]
            seconds = 0 if a == b else durations[a][b]
            # Unreachable pairs stay absent, so the planner never uses them.
            if seconds is not None: table[(origin['id'], destination['id'])] = seconds / 60
    return table


def confirm(stops, schedule, vehicle, *, origin=None, hazardous=False, cache_scope=None):
    """One truck-restricted route over the planned order; travel minutes for exactly those legs."""
    by_id = {s['id']: s for s in stops}
    ordered = ([{'id': '__driver__', 'address': origin}] if origin else []) + [by_id[row['stop_id']] for row in schedule]
    if len(ordered) < 2: return {}
    waypoints, pairs = [ordered[0]['address']], []
    for previous, stop in zip(ordered, ordered[1:]):
        if point(stop['address']) == point(previous['address']):
            pairs.append((previous['id'], stop['id'], None))
            continue
        waypoints.append(stop['address'])
        pairs.append((previous['id'], stop['id'], len(waypoints) - 2))
    legs = route(waypoints, vehicle, hazardous=hazardous, cache_scope=cache_scope)['legs'] if len(waypoints) > 1 else []
    return {(a, b): 0 if leg is None else legs[leg]['seconds'] / 60 for a, b, leg in pairs}
