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


def verify(text, postal_code):
    """Return one precise Canadian Google address match, or None."""
    # Address completion is independent of the commercial/dispatch road-routing
    # provider. A configured Google geocoding or Routes key is sufficient.
    key = os.environ.get('GOOGLE_GEOCODING_API_KEY') or os.environ.get('GOOGLE_ROUTES_API_KEY')
    if not key: return None
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
        if not postal or not city or not province or not part('street_number'): continue
        if postal_code and _postal(postal) != _postal(postal_code): continue
        if number and part('street_number') != number[1]: continue
        location = geometry.get('location', {})
        if isinstance(location.get('lat'), (int, float)) and isinstance(location.get('lng'), (int, float)):
            candidates.append({'text': match.get('formatted_address', text)[:500], 'latitude': location['lat'], 'longitude': location['lng'],
                'city': city, 'province': province, 'postal_code': postal})
    unique = {(row['latitude'], row['longitude'], row['postal_code']): row for row in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None
