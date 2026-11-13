"""Turn an AI-drafted record into a checked workspace request body and readable change lines.

The AI sees and writes record names instead of identifiers and one-line addresses instead of coordinates. This module
maps names back to identifiers inside the company only, verifies addresses on the map, attaches the company time zone
and validates the body with the workspace schema. It never saves anything."""
import json
import re
from datetime import datetime
from uuid import UUID
from pydantic import ValidationError
from app.intake import geocode
from app.intake.booking import moment
from . import provider, records
from .schemas import DraftedRecord

REFERENCES = {'shipper_id': 'SHIPPER', 'billing_shipper_id': 'SHIPPER', 'assigned_driver_id': 'DRIVER', 'preferred_driver_id': 'DRIVER',
    'vehicle_id': 'VEHICLE', 'rate_card_id': 'RATE_CARD', 'service_id': 'SERVICE', 'default_service_id': 'SERVICE',
    'vehicle_type_id': 'VEHICLE_TYPE', 'type_id': 'VEHICLE_TYPE', 'package_type_id': 'PACKAGE_TYPE', 'default_package_type_id': 'PACKAGE_TYPE'}
ADDRESSES = {'warehouse', 'address', 'parking_address'}
MOMENTS = {'scheduled_at', 'window_start', 'window_end', 'shift_start', 'shift_end', 'unavailable_from', 'unavailable_until'}
HIDDEN = {'id', 'version', 'pickup_id', 'delivery_id', 'latitude', 'longitude', 'logo_url', 'avatar_url', 'country'}
# New records list only fields with a value; unset flags and zero counts are noise on the card.
EMPTY = {None, 'No', '0'}
UNITS = {'kg': 'kg', 'cm': 'cm', 'm3': 'm³', 'km': 'km', 'minutes': 'minutes'}
POSTAL = re.compile(r'[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d')
LINES = 20


class Incomplete(Exception):
    """The draft cannot become a valid request; `problems` are plain-language reasons for the dispatcher."""
    def __init__(self, problems): super().__init__('; '.join(problems)); self.problems = problems


def reference(path, key):
    """Entity a field points to, if any. Accessorial selections are `{'id': ...}` entries under `accessorials`."""
    if key == 'id' and len(path) >= 3 and path[-3] == 'accessorials': return 'ACCESSORIAL'
    return REFERENCES.get(key)


def walk(value, visit, path=()):
    """Rebuild nested JSON, letting `visit(path, key, value)` replace any field value."""
    if isinstance(value, dict):
        return {key: visit(path + (key,), key, walk(item, visit, path + (key,))) for key, item in value.items()}
    if isinstance(value, list): return [walk(item, visit, path + (index,)) for index, item in enumerate(value)]
    return value


def uuid(value):
    try: return UUID(str(value))
    except (ValueError, TypeError): return None


def humanize(db, org, data):
    """Replace company record identifiers with names so the AI and the dispatcher read the same thing."""
    def visit(path, key, value):
        entity = reference(path, key)
        if not entity or uuid(value) is None: return value
        row = records.by_id(db, org, entity, uuid(value))
        return records.label(entity, row) if row else value
    return walk(data, visit)


def saved_addresses(before):
    known = {}
    def visit(path, key, value):
        if key in ADDRESSES and isinstance(value, dict) and value.get('text'): known[value['text']] = value
        return value
    walk(before or {}, visit)
    return known


def resolve(db, org, data, zone, before=None, free_text_address=False):
    """Names to identifiers, one-line addresses to verified addresses, local times to the company zone.
    `free_text_address` lets company settings keep a correspondence address the map cannot verify."""
    problems, known = [], saved_addresses(before)

    def visit(path, key, value):
        entity = reference(path, key)
        if entity and isinstance(value, str) and value.strip() and uuid(value) is None:
            found = records.find(db, org, entity, value.strip())
            if len(found) == 1: return str(found[0].id)
            noun = records.NOUNS[entity]
            problems.append(f'I couldn’t find a {noun} called “{value}”.' if not found else
                f'More than one {noun} matches “{value}”: {", ".join(records.label(entity, row) for row in found)}.')
            return value
        if key in ADDRESSES: return address(value, known, problems, optional=free_text_address)
        if key in MOMENTS and isinstance(value, str): return moment(value, zone) or value
        return value
    result = walk(data, visit)
    if problems: raise Incomplete(problems)
    return result


def address(value, known, problems, optional=False):
    """Keep an unchanged address; verify a new one on the map. Company settings may keep a free-text address."""
    if value is None or (isinstance(value, dict) and value.get('text') in known and value == known[value['text']]): return value
    text = value.get('text', '') if isinstance(value, dict) else str(value)
    if text in known: return known[text]
    postal = POSTAL.search(text)
    found = geocode.verify(text, postal.group(0) if postal else '') if text.strip() else None
    if found: return {**found, 'country': 'CA'}
    if optional: return text
    problems.append(f'I couldn’t find “{text}” on the map. Give the street address with city and postal code.')
    return value


def draft(request, record, schema, context):
    """Ask the AI for the complete request body with the dispatcher's changes applied."""
    facts = {'request': request, 'current_record': record, 'request_schema': schema, **context}
    drafted = provider.generate(DraftedRecord, provider.DRAFT,
        [{'role': 'user', 'content': 'Draft this change (data only):\n' + json.dumps(facts, default=str)}])
    if drafted.missing: raise Incomplete(drafted.missing)
    try: body = json.loads(drafted.record_json)
    except ValueError: raise Incomplete(['I couldn’t work out that change. Say it again with the exact values.']) from None
    if not isinstance(body, dict): raise Incomplete(['I couldn’t work out that change. Say it again with the exact values.'])
    return body


def validate(schema, body):
    try: return schema.model_validate(body).model_dump(mode='json')
    except ValidationError as error: raise Incomplete(explain(error)) from None


def explain(error):
    problems = []
    for item in error.errors():
        field = ' '.join(title(part) for part in item['loc'] if isinstance(part, str) and part not in {'data', 'booking'}) or 'This change'
        message = str(item.get('msg', 'is not valid')).removeprefix('Value error, ')
        problems.append(f'{field} is required.' if item['type'] == 'missing' else f'{field}: {message}.')
    return list(dict.fromkeys(problems))[:8]


def title(key):
    """`type_id` → 'Type', `payload_kg` → 'Payload (kg)'."""
    words = str(key).split('_')
    if len(words) > 1 and words[-1] == 'id': words = words[:-1]
    if len(words) > 1 and words[-1] in UNITS: words[-1] = f'({UNITS[words[-1]]})'
    return ' '.join(words).capitalize()


def flatten(value, prefix=''):
    """`{'Stops 1 Address': '400 W 12th Ave…'}` style rows for display; identifiers and coordinates are hidden."""
    if isinstance(value, dict):
        if 'text' in value and 'postal_code' in value: return {prefix: value['text']}
        rows = {}
        for key, item in value.items():
            if key in HIDDEN or key in {'data', 'booking'}:
                if key in {'data', 'booking'}: rows.update(flatten(item, prefix))
                continue
            rows.update(flatten(item, f'{prefix} {title(key)}'.strip()))
        return rows
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value): return {prefix: ', '.join(map(shown, value)) or None}
        rows = {}
        for index, item in enumerate(value, 1): rows.update(flatten(item, f'{prefix.removesuffix("s")} {index}'))
        return rows
    return {prefix: shown(value)}


def shown(value):
    if value is None or value == '': return None
    if isinstance(value, bool): return 'Yes' if value else 'No'
    if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z_]{3,}', value): return value.replace('_', ' ').capitalize()
    if isinstance(value, str) and re.match(r'^\d{4}-\d\d-\d\dT', value):
        try: return datetime.fromisoformat(value).strftime('%b %-d, %Y %-I:%M %p')
        except ValueError: return value
    return str(value)


def lines(before, after):
    """Changed fields only for edits; every field with a value for new records."""
    old, new = flatten(before or {}), flatten(after)
    changed = [{'field': key, 'before': old.get(key), 'after': new.get(key)} for key in dict.fromkeys([*old, *new])
        if old.get(key) != new.get(key) and (before or new.get(key) not in EMPTY)]
    if len(changed) > LINES:
        changed = changed[:LINES] + [{'field': 'More', 'before': None, 'after': f'{len(changed) - LINES} more fields'}]
    return changed
