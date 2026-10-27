"""Server-only TomTom truck routing. No contact details, fallback car routes or durable cache.

Exterior dimensions and a conservative loaded gross weight are explicit vehicle
facts, never inferred from cargo-space size or payload capacity.
"""
import json
from datetime import datetime, timezone
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from fastapi import HTTPException
from pydantic import ValidationError

from .schemas import TruckRoutingProfile

BASE = 'https://api.tomtom.com'
MAX_BYTES = 4_000_000


def number(value):
    if isinstance(value, bool): raise ValueError('Invalid routing number')
    result = float(value)
    if not math.isfinite(result) or result < 0: raise ValueError('Invalid routing number')
    return result


def point(address):
    try:
        if isinstance(address['latitude'], bool) or isinstance(address['longitude'], bool): raise ValueError()
        lat, lng = float(address['latitude']), float(address['longitude'])
        if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError()
        return {'latitude': lat, 'longitude': lng}
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, 'Verified stop coordinates are required for truck routing.') from None


def options(vehicle, *, hazardous=False):
    if hazardous:
        raise HTTPException(409, 'Dangerous goods need a classified routing profile; dispatcher review is required.')
    try:
        profile = TruckRoutingProfile.model_validate((vehicle or {}).get('road_profile'))
    except (ValidationError, TypeError):
        raise HTTPException(409, 'Save exterior dimensions and loaded gross weight in the vehicle’s selected Vehicle Type for truck routing.') from None
    if (vehicle or {}).get('payload_kg') is not None and profile.gross_weight_kg < number(vehicle['payload_kg']):
        raise HTTPException(409, 'Loaded gross weight must cover the vehicle payload capacity.')
    result = dict(travelMode='truck', vehicleCommercial=True, vehicleLength=float(profile.length_m),
        vehicleWidth=float(profile.width_m), vehicleHeight=float(profile.height_m), vehicleWeight=profile.gross_weight_kg)
    if profile.axle_weight_kg is not None: result['vehicleAxleWeight'] = profile.axle_weight_kg
    return result


def request(path, params, payload=None):
    key = os.environ.get('TOMTOM_API_KEY', '').strip()
    if not key:
        raise HTTPException(409, 'TomTom is not configured. Add the server API key before enabling truck routing.')
    query = urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in {**params, 'key': key}.items()})
    req = urllib.request.Request(BASE + path + '?' + query,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES: raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict): raise ValueError()
        return result
    except (OSError, ValueError):
        # Never propagate URLs, credentials, coordinates, provider bodies or quota text.
        raise HTTPException(503, 'TomTom routing is unavailable. Retry or request dispatcher review.') from None


def departure_value(departure):
    # Overdue bookings can retain their scheduled time; the provider needs current/future traffic.
    return departure.isoformat() if departure and departure > datetime.now(timezone.utc) else 'now'


def route(addresses, vehicle, *, departure=None, hazardous=False, geometry=False):
    params = {**options(vehicle, hazardous=hazardous), 'traffic': 'true', 'routeType': 'fastest',
        'departAt': departure_value(departure), 'computeTravelTimeFor': 'all',
        'routeRepresentation': 'polyline' if geometry else 'summaryOnly', 'maxAlternatives': 0,
        'sectionType': 'travelMode'}
    if not 2 <= len(addresses) <= 150:
        raise HTTPException(409, 'TomTom route display supports 2–150 locations.')
    points = [point(address) for address in addresses]
    heading = addresses[0].get('heading')
    if heading is not None:
        try:
            heading = number(heading)
            if heading >= 360: raise ValueError()
            params['vehicleHeading'] = int(heading)
        except (ValueError, TypeError): raise HTTPException(409, 'Driver heading is invalid.') from None
    locations = ':'.join(f"{p['latitude']},{p['longitude']}" for p in points)
    result = request('/routing/1/calculateRoute/' + locations + '/json', params)
    try:
        row = result['routes'][0]
        legs = row['legs']
        if len(legs) != len(points) - 1: raise ValueError()
        output = []
        shape = []
        for leg in legs:
            summary = leg['summary']
            output.append(dict(seconds=number(summary['travelTimeInSeconds']),
                delay_seconds=number(summary['trafficDelayInSeconds']), metres=number(summary['lengthInMeters'])))
            if geometry:
                for p in leg['points']:
                    p = point(p)
                    xy = [p['latitude'], p['longitude']]
                    if not shape or shape[-1] != xy: shape.append(xy)
        if geometry and len(shape) < 2: raise ValueError()
        # An explicit zero speed/length/weight must not become an apparently valid route.
        if any(section.get('sectionType') == 'TRAVEL_MODE' and section.get('travelMode') == 'other'
               for section in row.get('sections', [])): raise ValueError()
        return {'legs': output, 'points': shape}
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, HTTPException):
        raise HTTPException(503, 'TomTom returned an incomplete truck route; dispatcher review is required.') from None


def matrix(db, stops, vehicle, *, departure=None, hazardous=False):
    params = options(vehicle, hazardous=hazardous)
    # Bound cost and latency before issuing any provider request. Each cell is a route calculation.
    if len(stops) > 50:
        raise HTTPException(409, 'TomTom planning supports at most 50 locations including the driver origin; split the route.')
    points = [dict(point=point(stop['address'])) for stop in stops]
    opts = {**params, 'traffic': 'live', 'routeType': 'fastest',
        'departAt': departure_value(departure)}
    key = json.dumps([stops, opts], sort_keys=True, default=str)
    cache = db.info.setdefault('tomtom_road_matrices', {}) if db is not None else {}
    if key in cache: return cache[key]
    table = {}
    for a in range(0, len(stops), 10):
        for b in range(0, len(stops), 10):
            origins, destinations = stops[a:a+10], stops[b:b+10]
            result = request('/routing/matrix/2', {}, dict(origins=points[a:a+10], destinations=points[b:b+10], options=opts))
            try:
                seen = set()
                for cell in result['data']:
                    i, j = cell['originIndex'], cell['destinationIndex']
                    if type(i) is not int or type(j) is not int or not 0 <= i < len(origins) or not 0 <= j < len(destinations): raise ValueError()
                    if (i, j) in seen: raise ValueError()
                    seen.add((i, j))
                    if 'detailedError' in cell:
                        error = cell['detailedError']
                        code = error.get('innerError', {}).get('code', error.get('code'))
                        if code in {'NO_ROUTE_FOUND', 'MAP_MATCHING_FAILURE'}: continue
                        raise ValueError()
                    seconds = number(cell['routeSummary']['travelTimeInSeconds'])
                    table[(origins[i]['id'], destinations[j]['id'])] = seconds / 60
                if len(seen) != len(origins) * len(destinations): raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise HTTPException(503, 'TomTom returned incomplete truck travel times.') from None
    if len(cache) < 64: cache[key] = table
    return table
