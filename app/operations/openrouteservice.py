"""Bounded development HGV routing using vehicle-restricted Directions, never the unrestricted Matrix API."""
import json
import hashlib
import os
import urllib.error
import urllib.request
from fastapi import HTTPException
from .truck_profile import number, point, profile

BASE = 'https://api.heigit.org/openrouteservice/v2/directions/driving-hgv/geojson'
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


def request(payload):
    key = os.environ.get('ORS_API_KEY', '').strip()
    if not key:
        raise HTTPException(409, 'openrouteservice is not configured. Add the server API key before assigning orders.')
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(),
        headers={'Authorization': key, 'Content-Type': 'application/json', 'Accept': 'application/geo+json'})
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
    opts = restrictions(vehicle, hazardous)
    # At most 30 directed pairs: keep individual development plans within the free minute quota.
    if len(stops) > 6:
        raise HTTPException(409, 'Development truck routing supports at most 6 locations including the driver. Split the route or enable TomTom for larger plans.')
    verified = {s['id']: point(s['address']) for s in stops}
    cache = db.info.setdefault('ors_truck_pairs', {}) if db is not None else {}
    table = {}
    for origin in stops:
        for destination in stops:
            pair = (origin['id'], destination['id'])
            if pair[0] == pair[1] or verified[pair[0]] == verified[pair[1]]:
                table[pair] = 0
                continue
            coordinates = [origin['address'], destination['address']]
            key = json.dumps([str(cache_scope), coordinates, opts, hashlib.sha256(os.environ.get('ORS_API_KEY', '').encode()).hexdigest()], sort_keys=True)
            if key not in cache:
                try:
                    cache[key] = route(coordinates, vehicle, hazardous=hazardous, cache_scope=cache_scope)['legs'][0]['seconds'] / 60
                except HTTPException as error:
                    if error.status_code != 409 or not error.detail.startswith('No route was found'): raise
                    cache[key] = None
            if cache[key] is not None: table[pair] = cache[key]
    return table
