"""Static tracking map: one Google Static Maps image per Order, drawn from the same tracking projection.

The image shows only what `order_tracking` returns to this actor (the Order's own stops, and the driver's position
only when tracking allows it), so a Shipper never receives more than the tracking view. The key and the optional
URL-signing secret stay on the server. Road geometry is cached per Order version and images per exact map URL.
"""
import base64
import hashlib
import hmac
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from fastapi import HTTPException
from app.operations.schemas import Booking
from .orders import visible_order
from .tracking import order_tracking
from .travel import order_road_path, provider

BASE = 'https://maps.googleapis.com'
ENDPOINT = '/maps/api/staticmap'
IMAGE_SECONDS, MAX_URL, MAX_BYTES = 60, 8000, 2_000_000
# Same colours as the tracking stop list (pickup slate, delivery blue); the driver stands out in amber.
COLORS = {'PICKUP': '0x334155', 'DROPOFF': '0x2563eb', 'DRIVER': '0xf59e0b'}
LABELS = {'PICKUP': 'P', 'DROPOFF': 'D', 'DRIVER': 'T'}


class Cache:
    """Small in-process LRU with an optional time limit; enough for one API process."""
    def __init__(self, size, seconds=None): self.rows, self.size, self.seconds = OrderedDict(), size, seconds

    def get(self, key):
        row = self.rows.get(key)
        if row is None or (self.seconds and time.monotonic() - row[0] > self.seconds): return None
        self.rows.move_to_end(key)
        return row[1]

    def put(self, key, value):
        self.rows[key] = (time.monotonic(), value)
        self.rows.move_to_end(key)
        while len(self.rows) > self.size: self.rows.popitem(last=False)


paths, images = Cache(500), Cache(300, IMAGE_SECONDS)


def encode_polyline(points):
    """[lat, lng] pairs to a Google encoded polyline (precision 5)."""
    out, last = [], (0, 0)
    for lat, lng in points:
        current = (round(lat * 1e5), round(lng * 1e5))
        for value in (current[0] - last[0], current[1] - last[1]):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1f)) + 63)); value >>= 5
            out.append(chr(value + 63))
        last = current
    return ''.join(out)


def _road(db, actor, order):
    key = (str(order.organization_id), str(order.id), order.version)
    cached = paths.get(key)
    if cached is None:
        try: points = order_road_path(db, actor, Booking.model_validate(order.facts))
        except HTTPException: points = []
        # Keep the URL well under Google's limit by thinning long routes evenly; ends are always kept.
        while len(encode_polyline(points)) > MAX_URL // 2 and len(points) > 2: points = points[::2] + ([points[-1]] if len(points) % 2 == 0 else [])
        cached = encode_polyline(points) if len(points) > 1 else ''
        paths.put(key, cached)
    return cached


def sign(path_and_query, secret):
    digest = hmac.new(base64.urlsafe_b64decode(secret), path_and_query.encode(), hashlib.sha1).digest()
    return path_and_query + '&signature=' + base64.urlsafe_b64encode(digest).decode()


def map_url(tracking, road, key, secret=None):
    point = lambda lat, lng: f'{lat:.6f},{lng:.6f}'
    query = [('size', '640x320'), ('scale', '2'), ('maptype', 'roadmap'), ('language', 'en'), ('region', 'CA')]
    if road: query.append(('path', f'color:0x2563eb99|weight:5|enc:{road}'))
    for kind in ('PICKUP', 'DROPOFF'):
        spots = [point(s['address']['latitude'], s['address']['longitude']) for s in tracking['stops']
            if s['kind'] == kind and s['address'].get('latitude') is not None and s['address'].get('longitude') is not None]
        if spots: query.append(('markers', '|'.join([f'color:{COLORS[kind]}', f'label:{LABELS[kind]}', *spots])))
    if tracking['location']:
        query.append(('markers', f'color:{COLORS["DRIVER"]}|label:{LABELS["DRIVER"]}|' + point(tracking['location']['latitude'], tracking['location']['longitude'])))
    query.append(('key', key))
    path_and_query = ENDPOINT + '?' + urllib.parse.urlencode(query, safe=':|,')
    return BASE + (sign(path_and_query, secret) if secret else path_and_query)


def tracking_map(db, actor, identity):
    """PNG bytes of the Order's tracking map, or 409 when maps are off / nothing can be placed, 503 when Google fails."""
    key = os.environ.get('GOOGLE_MAPS_STATIC_API_KEY') or os.environ.get('GOOGLE_ROUTES_API_KEY')
    if provider() != 'google' or not key: raise HTTPException(409, 'Maps are not configured.')
    tracking = order_tracking(db, actor, identity)
    if not any(s['address'].get('latitude') is not None for s in tracking['stops']) and not tracking['location']:
        raise HTTPException(409, 'This Order has no map position yet.')
    road = _road(db, actor, visible_order(db, actor, identity))
    url = map_url(tracking, road, key, os.environ.get('GOOGLE_MAPS_URL_SIGNING_SECRET'))
    image = images.get(url)
    if image is None:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                if not response.headers.get('Content-Type', '').startswith('image/'): raise ValueError()
                image = response.read(MAX_BYTES + 1)
        except (OSError, ValueError): raise HTTPException(503, 'The map could not be loaded.') from None
        if len(image) > MAX_BYTES: raise HTTPException(503, 'The map could not be loaded.')
        images.put(url, image)
    return image
