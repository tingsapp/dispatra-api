"""Resolve an emailed Canadian street/city with one precise Google match. Supplied postal codes
must agree; missing province/postal code comes from the map, never a guess."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

PRECISE = {'ROOFTOP', 'RANGE_INTERPOLATED'}


def same_city(supplied, resolved):
    """Allow one spelling edit only after a unique precise map match (Bunaby -> Burnaby)."""
    left, right = supplied.strip().casefold(), resolved.strip().casefold()
    if left == right: return True
    if min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 1: return False
    if len(left) == len(right):
        differences = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(differences) == 1 or (len(differences) == 2 and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]] and left[differences[1]] == right[differences[0]])
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def _postal(value):
    return ''.join(value.split()).upper()


def _one(candidates):
    unique = {(row['latitude'], row['longitude'], row['postal_code']): row for row in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _candidate(*, text, postal_code, number, formatted, city, province, postal, street_number, country, latitude, longitude):
    if country != 'CA' or not postal or not city or not province or not street_number: return None
    if postal_code and _postal(postal) != _postal(postal_code): return None
    if number and street_number != number[1]: return None
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)): return None
    return {'text': (formatted or text)[:500], 'latitude': latitude, 'longitude': longitude,
        'city': city, 'province': province, 'postal_code': postal}


def _geocoding(text, postal_code, key):
    query = urllib.parse.urlencode({'address': text, 'components': 'country:CA', 'key': key})
    try:
        with urllib.request.urlopen('https://maps.googleapis.com/maps/api/geocode/json?' + query, timeout=15) as response:
            result = json.loads(response.read(1_000_000))
    except (OSError, ValueError): return None
    candidates = []
    number = re.match(r'\s*(\d+)\b', text)
    for match in result.get('results', []) if result.get('status') == 'OK' else []:
        geometry = match.get('geometry', {})
        parts = match.get('address_components', [])
        def part(kind, short=False):
            return next((component.get('short_name' if short else 'long_name', '') for component in parts if kind in component.get('types', [])), '')
        postal = part('postal_code')
        city = part('locality') or part('postal_town')
        province = part('administrative_area_level_1', True)
        if match.get('partial_match') or geometry.get('location_type') not in PRECISE or part('country', True) != 'CA': continue
        location = geometry.get('location', {})
        candidate = _candidate(text=text, postal_code=postal_code, number=number, formatted=match.get('formatted_address'),
            city=city, province=province, postal=postal, street_number=part('street_number'), country=part('country', True),
            latitude=location.get('lat'), longitude=location.get('lng'))
        if candidate: candidates.append(candidate)
    return _one(candidates)


def _places(text, postal_code, key):
    """Resolve one Canadian street address through Places Text Search (New).

    Routes keys frequently enable this endpoint while not being authorized for
    the legacy Geocoding API. It provides the same exact fields we need without
    exposing the server key to the browser.
    """
    request = urllib.request.Request('https://places.googleapis.com/v1/places:searchText',
        data=json.dumps({'textQuery': text, 'regionCode': 'CA'}).encode(),
        headers={'Content-Type': 'application/json', 'X-Goog-Api-Key': key,
            'X-Goog-FieldMask': 'places.formattedAddress,places.location,places.addressComponents'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(1_000_000))
    except (OSError, ValueError): return None
    number = re.match(r'\s*(\d+)\b', text)
    candidates = []
    for match in result.get('places', []):
        parts = match.get('addressComponents', [])
        def part(kind, short=False):
            return next((component.get('shortText' if short else 'longText', '') for component in parts if kind in component.get('types', [])), '')
        location = match.get('location', {})
        candidate = _candidate(text=text, postal_code=postal_code, number=number, formatted=match.get('formattedAddress'),
            city=part('locality') or part('postal_town'), province=part('administrative_area_level_1', True), postal=part('postal_code'),
            street_number=part('street_number'), country=part('country', True), latitude=location.get('latitude'), longitude=location.get('longitude'))
        if candidate: candidates.append(candidate)
    return _one(candidates)


def verify(text, postal_code):
    """Return one precise Canadian Google address match, or None."""
    geocoding_key = os.environ.get('GOOGLE_GEOCODING_API_KEY')
    routes_key = os.environ.get('GOOGLE_ROUTES_API_KEY')
    if geocoding_key:
        resolved = _geocoding(text, postal_code, geocoding_key)
        if resolved: return resolved
    # Use a server-side Places fallback for the existing Routes credential. The
    # fallback is deliberately not attempted with a browser-only Maps key.
    return _places(text, postal_code, routes_key) if routes_key else None
