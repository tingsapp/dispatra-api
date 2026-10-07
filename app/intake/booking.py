"""Deterministic step after extraction: map facts onto the shared `Booking` contract and list what is missing.

Nothing here guesses. Units are converted exactly, addresses must carry a valid postal code and verified
coordinates, and the result is validated by the same `Booking` schema the portals use.
"""
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from app.operations.schemas import Booking

POSTAL = re.compile(r'^[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d$')
PROVINCES = {'AB': 'alberta', 'BC': 'british columbia', 'MB': 'manitoba', 'NB': 'new brunswick', 'NL': 'newfoundland and labrador',
    'NS': 'nova scotia', 'NT': 'northwest territories', 'NU': 'nunavut', 'ON': 'ontario', 'PE': 'prince edward island',
    'QC': 'quebec', 'SK': 'saskatchewan', 'YT': 'yukon'}
ALIASES = {'b.c.': 'BC', 'p.e.i.': 'PE', 'pei': 'PE', 'newfoundland': 'NL', 'québec': 'QC', 'nwt': 'NT', 'yukon territory': 'YT'}
KG_PER_LB, CM_PER_IN = Decimal('0.45359237'), Decimal('2.54')


def province(value):
    text = (value or '').strip().lower()
    if text.upper() in PROVINCES: return text.upper()
    return ALIASES.get(text) or next((code for code, name in PROVINCES.items() if name == text), None)


def postal(value):
    text = ''.join((value or '').split()).upper()
    return f'{text[:3]} {text[3:]}' if POSTAL.match(text) else None


def moment(value, zone):
    if not value: return None
    try: parsed = datetime.fromisoformat(value)
    except ValueError: return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).isoformat()


def measure(value, factor=Decimal('1')):
    if value is None or value <= 0: return None
    return str((Decimal(str(value)) * factor).quantize(Decimal('0.01'), ROUND_HALF_UP))


def pick(value, options):
    """Exact code or name match among active catalogue entries; never a fuzzy guess."""
    text = (value or '').strip().lower()
    return next((o['id'] for o in options if text and text in {o['code'].lower(), o['name'].lower()}), None)


def address(stop, number, context, verify, missing):
    label = f'Stop {number}'
    if stop.use_shipper_warehouse and stop.kind == 'PICKUP':
        if context['shipper']: return dict(context['shipper']['warehouse'])
        missing.append(f"{label}: the Shipper's warehouse address")
        return None
    found = stop.address
    street, city = (found.street or '').strip() if found else '', (found.city or '').strip() if found else ''
    code, zip_code = province(found.province if found else None), postal(found.postal_code if found else None)
    if not (street and city and code and zip_code):
        missing.append(f'{label}: complete address with street, city, province and postal code')
        return None
    text = f'{street}, {city}, {code} {zip_code}, Canada'
    verified = verify(text, zip_code)
    if verified is None:
        missing.append(f'{label}: address could not be verified on the map')
        return {'text': text, 'city': city, 'province': code, 'postal_code': zip_code, 'country': 'CA'}
    return {'text': verified['text'], 'city': city, 'province': code, 'postal_code': zip_code, 'country': 'CA',
        'latitude': verified['latitude'], 'longitude': verified['longitude']}


def build(extraction, context, verify):
    """Return (draft, missing). `draft` is a JSON Booking body; it is complete only when `missing` is empty.
    Without a matched Shipper (`context['shipper']` is None) the draft has no Shipper and no warehouse pickup address."""
    zone, missing = ZoneInfo(context['time_zone']), []
    service = pick(extraction.service, context['services']) or context.get('default_service_id')
    if service is None and len(context['services']) == 1: service = context['services'][0]['id']
    if service is None: missing.append('Service level')
    stops = []
    for number, stop in enumerate(extraction.stops, 1):
        place = address(stop, number, context, verify, missing)
        stops.append({'id': str(uuid4()), 'kind': stop.kind, 'address': place,
            'contact_name': (stop.contact_name or '')[:160], 'phone': (stop.phone or '')[:50], 'instructions': (stop.instructions or '')[:2000],
            'window_start': moment(stop.window_start, zone), 'window_end': moment(stop.window_end, zone)})
    pickups = [s for s in stops if s['kind'] == 'PICKUP']
    dropoffs = [s for s in stops if s['kind'] == 'DROPOFF']
    if not pickups: missing.append('Pickup address')
    if not dropoffs: missing.append('Delivery address')
    scheduled = moment(extraction.scheduled_at, zone) or next((s['window_start'] for s in pickups if s['window_start']), None)
    if scheduled is None: missing.append('Pickup date and time')
    items = []
    for number, item in enumerate(extraction.items, 1):
        label = f'Item {number}'
        def stop_id(index, kind, fallback):
            if 0 <= index < len(stops) and stops[index]['kind'] == kind: return stops[index]['id']
            return fallback[0]['id'] if len(fallback) == 1 else None
        pickup_id, delivery_id = stop_id(item.pickup_index, 'PICKUP', pickups), stop_id(item.delivery_index, 'DROPOFF', dropoffs)
        if (pickups and dropoffs) and not (pickup_id and delivery_id): missing.append(f'{label}: which pickup and delivery it belongs to')
        weight = measure(item.weight_each, KG_PER_LB if item.weight_unit == 'lb' else Decimal('1')) if item.weight_unit else None
        factor = CM_PER_IN if item.dimension_unit == 'in' else Decimal('1')
        sizes = [measure(v, factor) if item.dimension_unit else None for v in (item.length, item.width, item.height)]
        if not item.quantity or item.quantity < 1: missing.append(f'{label}: quantity')
        if weight is None: missing.append(f'{label}: weight')
        if None in sizes: missing.append(f'{label}: length, width and height')
        items.append({'id': str(uuid4()), 'pickup_id': pickup_id, 'delivery_id': delivery_id, 'quantity': item.quantity,
            'weight_kg': weight, 'length_cm': sizes[0], 'width_cm': sizes[1], 'height_cm': sizes[2],
            'pallets': max(0, item.pallets_each or 0), 'fragile': item.fragile, 'dangerous_goods': item.dangerous_goods,
            'description': (item.description or '')[:2000]})
    if not items: missing.append('Items: quantity, weight and dimensions')
    linked = {i['pickup_id'] for i in items} | {i['delivery_id'] for i in items}
    for number, stop in enumerate(stops, 1):
        if stop['id'] not in linked and items: missing.append(f'Stop {number}: no item is picked up or delivered there')
    draft = {'shipper_id': context['shipper']['id'] if context['shipper'] else None, 'service_id': service,
        'vehicle_type_id': pick(extraction.vehicle_type, context['vehicle_types']), 'scheduled_at': scheduled,
        'stops': stops, 'items': items, 'external_reference': (extraction.reference or '')[:160]}
    if not missing:
        try: Booking.model_validate(draft)
        except ValidationError as error:
            missing += sorted({'Booking: ' + str(e.get('msg', 'invalid value')).removeprefix('Value error, ') for e in error.errors()})
    return draft, list(dict.fromkeys(missing))
