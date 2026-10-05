"""Verify an emailed Canadian address and obtain its coordinates. Only a precise Google match whose postal code
agrees with the email is accepted; anything else stays unknown and the email becomes a draft."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from app.operations.travel import provider

PRECISE = {'ROOFTOP', 'RANGE_INTERPOLATED'}


def _postal(value):
    return ''.join(value.split()).upper()


def verify(text, postal_code):
    """Return {'text', 'latitude', 'longitude'} or None. No external call outside the Google provider."""
    key = os.environ.get('GOOGLE_GEOCODING_API_KEY') or os.environ.get('GOOGLE_ROUTES_API_KEY')
    if provider() != 'google' or not key: return None
    query = urllib.parse.urlencode({'address': text, 'components': 'country:CA', 'key': key})
    try:
        with urllib.request.urlopen('https://maps.googleapis.com/maps/api/geocode/json?' + query, timeout=15) as response:
            result = json.loads(response.read(1_000_000))
    except (OSError, ValueError): return None
    for match in result.get('results', [])[:3] if result.get('status') == 'OK' else []:
        geometry = match.get('geometry', {})
        postal = next((c.get('long_name', '') for c in match.get('address_components', []) if 'postal_code' in c.get('types', [])), '')
        if geometry.get('location_type') not in PRECISE or _postal(postal) != _postal(postal_code): continue
        location = geometry.get('location', {})
        if isinstance(location.get('lat'), (int, float)) and isinstance(location.get('lng'), (int, float)):
            return {'text': match.get('formatted_address', text)[:500], 'latitude': location['lat'], 'longitude': location['lng']}
    return None
