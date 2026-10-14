"""TMS order intake. Payloads are stored first (webhook or polling), then mapped and applied by the worker.

Mapping and geocoding run between short transactions. Orders are created, updated or cancelled only through the order
use cases with a `SYSTEM` dispatcher actor, so pricing, validation, idempotency, audit and events apply unchanged.
Anything the map cannot resolve leaves the payload in NEEDS_REVIEW with reasons; nothing is guessed.
"""
import hashlib
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.database import context, engine
from app.intake import booking as normalize, geocode
from app.models import now
from app.operations import directory, orders
from app.operations.common import settings
from app.operations.models import Catalog, Order, RateCard, Shipper
from app.operations.schemas import Booking, OrderUpdate, ShipperData, Version
from .fleet import actor
from .mapping import boolean, fingerprint, get, number, read, records, text
from .models import TmsConnection, TmsInbox, TmsLink

MAX_ATTEMPTS = 3
NAMESPACE = UUID('6b1f3c52-5d6e-4b0e-9a63-2f4c1d7e8a90')


def stable(organization_id, *parts) -> UUID:
    """Deterministic stop/item IDs, so a TMS update keeps the same Dispatra identities."""
    return uuid5(NAMESPACE, ':'.join(map(str, (organization_id, *parts))))


def _key(*parts) -> str:
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()


def orders_in(payload, mapping: dict) -> list[dict]:
    """One order object, or a list of them (bare array or under a common key)."""
    if isinstance(payload, dict) and text(read(payload, mapping.get('id'))): return [payload]
    return records(payload)


def store(db, organization_id: UUID, payload, mapping: dict, via: str) -> tuple[int, int]:
    """Insert each order once per distinct payload. Caller holds the transaction and company context. (stored, rejected)."""
    stored = rejected = 0
    for order in orders_in(payload, mapping):
        external = text(read(order, mapping.get('id')), 200)
        if not external:
            rejected += 1
            continue
        identity = db.scalar(insert(TmsInbox).values(organization_id=organization_id, external_id=external, revision=fingerprint(order),
            received_via=via, payload=order, status='RECEIVED', missing=[], attempts=0, event='order.updated', version=1, created_at=now(),
            updated_at=now(), received_at=now(), id=uuid4())
            .on_conflict_do_nothing(constraint='tms_inbox_revision').returning(TmsInbox.id))
        stored += identity is not None
    return stored, rejected


# ---- Mapping ---------------------------------------------------------------------------------------------------

def _address(source, mapping, prefix, label, missing, required=True):
    street, city = text(get(source, mapping, f'{prefix}address')), text(get(source, mapping, f'{prefix}city'), 160)
    code = normalize.province(text(get(source, mapping, f'{prefix}province')))
    postal = normalize.postal(text(get(source, mapping, f'{prefix}postal_code')))
    if not (street and city and code and postal):
        if required or street or city: missing.append(f'{label}: complete address with street, city, province and postal code')
        return None
    full = f'{street}, {city}, {code} {postal}, Canada'
    verified = geocode.verify(full, postal)
    if verified is None:
        missing.append(f'{label}: address could not be verified on the map')
        return {'text': full, 'city': city, 'province': code, 'postal_code': postal, 'country': 'CA'}
    return {'text': verified['text'], 'city': city, 'province': code, 'postal_code': postal, 'country': 'CA',
        'latitude': verified['latitude'], 'longitude': verified['longitude']}


def _measure(value, factor):
    amount = number(value)
    return str((amount * factor).quantize(Decimal('0.01'))) if amount is not None and amount > 0 else None


def _stops(order, mapping, ctx, external, missing):
    rows = read(order, mapping.get('stops'))
    if not isinstance(rows, list) or len(rows) < 2:
        missing.append('Stop list with at least one pickup and one drop-off')
        return [], {}
    pickup, dropoff = (mapping.get('stop.pickup_value') or '').strip().lower(), (mapping.get('stop.delivery_value') or '').strip().lower()
    stops, by_tms_id = [], {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            missing.append(f'Stop {index}: not an object')
            continue
        value = text(get(row, mapping, 'stop.type')).lower()
        kind = 'PICKUP' if value and value == pickup else 'DROPOFF' if value and value == dropoff else None
        if kind is None: missing.append(f'Stop {index}: type {value!r} is neither the pickup nor the drop-off value')
        tms_id = text(get(row, mapping, 'stop.id'), 200) or f'#{index}'
        address = _address(row, mapping, 'stop.', f'Stop {index}', missing)
        stop = {'id': str(stable(ctx['organization_id'], external, 'stop', tms_id)), 'kind': kind or 'PICKUP', 'address': address,
            'contact_name': text(get(row, mapping, 'stop.contact'), 160), 'phone': text(get(row, mapping, 'stop.phone'), 50),
            'instructions': text(get(row, mapping, 'stop.instructions'), 2000),
            'window_start': normalize.moment(text(get(row, mapping, 'stop.window_start')), ctx['zone']),
            'window_end': normalize.moment(text(get(row, mapping, 'stop.window_end')), ctx['zone'])}
        stops.append(stop)
        by_tms_id[tms_id] = stop
    return stops, by_tms_id


def _items(order, mapping, ctx, external, stops, by_tms_id, missing):
    rows = read(order, mapping.get('items'))
    if not isinstance(rows, list) or not rows:
        missing.append('Item list with at least one item')
        return []
    pickups, dropoffs = [s for s in stops if s['kind'] == 'PICKUP'], [s for s in stops if s['kind'] == 'DROPOFF']
    items = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            missing.append(f'Item {index}: not an object')
            continue
        refs = [text(get(row, mapping, key), 200) for key in ('item.pickup_stop', 'item.delivery_stop')]
        if all(refs):
            found = [by_tms_id.get(ref) for ref in refs]
            if not found[0] or found[0]['kind'] != 'PICKUP' or not found[1] or found[1]['kind'] != 'DROPOFF':
                missing.append(f'Item {index}: pickup or drop-off stop ID does not match a stop of that type')
                continue
            link = found
        elif len(pickups) == 1 and len(dropoffs) == 1: link = [pickups[0], dropoffs[0]]
        else:
            missing.append(f'Item {index}: pickup and drop-off stop IDs are needed when an order has several stops')
            continue
        size = {key: _measure(get(row, mapping, f'item.{key}'), ctx['weight' if key == 'weight' else 'size']) for key in ('weight', 'length', 'width', 'height')}
        for key, value in size.items():
            if value is None: missing.append(f'Item {index}: {key}')
        quantity, pallets = number(get(row, mapping, 'item.quantity')), number(get(row, mapping, 'item.pallets'))
        items.append({'id': str(stable(ctx['organization_id'], external, 'item', index)), 'pickup_id': link[0]['id'], 'delivery_id': link[1]['id'],
            'quantity': int(quantity) if quantity and quantity >= 1 else 1, 'weight_kg': size['weight'], 'length_cm': size['length'],
            'width_cm': size['width'], 'height_cm': size['height'], 'pallets': int(pallets) if pallets and pallets > 0 else 0,
            'fragile': bool(boolean(get(row, mapping, 'item.fragile'))), 'dangerous_goods': bool(boolean(get(row, mapping, 'item.dangerous_goods'))),
            'description': text(get(row, mapping, 'item.description'), 2000)})
    return items


def build(order: dict, ctx: dict) -> dict:
    """Map one TMS order. Returns {'external', 'cancelled', 'booking', 'shipper', 'missing'}; calls geocoding."""
    mapping, missing = ctx['map'], []
    external = text(get(order, mapping, 'id'), 160)
    cancel_value = (mapping.get('cancelled_value') or '').strip().lower()
    if cancel_value and text(get(order, mapping, 'status')).lower() == cancel_value:
        return {'external': external, 'cancelled': True, 'booking': None, 'shipper': None, 'missing': []}
    scheduled = normalize.moment(text(get(order, mapping, 'scheduled_at')), ctx['zone'])
    if not scheduled: missing.append('Scheduled time')
    service_value = text(get(order, mapping, 'service'))
    service = normalize.pick(service_value, ctx['services'])
    if not service: missing.append(f'Service level {service_value!r} does not match a Dispatra service' if service_value else 'Service level')
    type_value = text(get(order, mapping, 'vehicle_type'))
    vehicle_type = normalize.pick(type_value, ctx['vehicle_types']) if type_value else None
    if type_value and not vehicle_type: missing.append(f'Vehicle type {type_value!r} does not match a Dispatra vehicle type')
    total, tax = number(get(order, mapping, 'total')), number(get(order, mapping, 'tax'))
    if total is None or total < 0: missing.append('Total price')
    if tax is None or tax < 0: missing.append('Tax')
    elif total is not None and tax > total: missing.append('Tax is more than the total price')
    if not ctx['rate_card_id']: missing.append('An active Imported price rate card')
    shipper = {'id': text(get(order, mapping, 'shipper.id'), 200), 'name': text(get(order, mapping, 'shipper.name'), 160),
        'company_name': text(get(order, mapping, 'shipper.company_name'), 160), 'email': text(get(order, mapping, 'shipper.email'), 254).lower(),
        'phone': text(get(order, mapping, 'shipper.phone'), 50), 'address': None}
    for key, label in (('id', 'Shipper ID'), ('name', 'Shipper name'), ('email', 'Shipper email')):
        if not shipper[key]: missing.append(label)
    stops, by_tms_id = _stops(order, mapping, ctx, external, missing)
    shipper['address'] = _address(order, mapping, 'shipper.', 'Shipper', [], required=False) or next(
        (s['address'] for s in stops if s['kind'] == 'PICKUP' and s['address'] and s['address'].get('latitude') is not None), None)
    items = _items(order, mapping, ctx, external, stops, by_tms_id, missing)
    draft = {'service_id': service, 'vehicle_type_id': vehicle_type, 'rate_card_id': ctx['rate_card_id'], 'scheduled_at': scheduled,
        'stops': stops, 'items': items, 'imported_total': str(total) if total is not None else None, 'imported_tax': str(tax) if tax is not None else None,
        'external_reference': external, 'internal_notes': text(get(order, mapping, 'notes'), 2000)}
    return {'external': external, 'cancelled': False, 'booking': draft, 'shipper': shipper, 'missing': missing}


# ---- Processing ------------------------------------------------------------------------------------------------

def _context(db, organization_id):
    connection = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == organization_id))
    _, config = settings(db, actor(organization_id))
    def choices(kind):
        return [{'id': str(row.id), 'code': row.code, 'name': row.data.get('name', row.code)} for row in db.scalars(select(Catalog).where(
            Catalog.organization_id == organization_id, Catalog.kind == kind, Catalog.active.is_(True)))]
    card = db.scalar(select(RateCard.id).where(RateCard.organization_id == organization_id, RateCard.active.is_(True),
        RateCard.data['method'].astext == 'IMPORTED').order_by(RateCard.is_default.desc(), RateCard.created_at, RateCard.id).limit(1))
    return {'organization_id': organization_id, 'map': (connection.field_maps if connection else {}).get('order', {}),
        'zone': ZoneInfo(config.time_zone), 'weight': Decimal('0.45359237') if config.weight_unit == 'lb' else Decimal('1'),
        'size': Decimal('2.54') if config.dimension_unit == 'in' else Decimal('1'), 'services': choices('SERVICE'),
        'vehicle_types': choices('VEHICLE_TYPE'), 'rate_card_id': str(card) if card else None}


def _claim(organization_id, inbox_id):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = db.scalar(select(TmsInbox).where(TmsInbox.organization_id == organization_id, TmsInbox.id == inbox_id,
            TmsInbox.status == 'RECEIVED').with_for_update(skip_locked=True))
        if row is None or row.attempts >= MAX_ATTEMPTS: return None
        row.attempts += 1
        return {'payload': row.payload, 'ctx': _context(db, organization_id)}


def _link(db, organization_id, kind, external):
    return db.scalar(select(TmsLink).where(TmsLink.organization_id == organization_id, TmsLink.kind == kind, TmsLink.external_id == external))


def _shipper(db, organization_id, found: dict, key_parts) -> Shipper:
    """Linked by TMS shipper ID, else matched by email, else created with the mapped or first-pickup address."""
    link = _link(db, organization_id, 'SHIPPER', found['id'])
    row = db.get(Shipper, link.record_id) if link else db.scalar(select(Shipper).where(Shipper.organization_id == organization_id,
        func.lower(Shipper.email) == found['email'], Shipper.warehouse.is_not(None), Shipper.archived_at.is_(None)).order_by(Shipper.created_at).limit(1))
    if row is None:
        if not found['address']: raise HTTPException(422, 'Shipper address, or a verified pickup address for a new shipper')
        data = ShipperData(name=found['name'], kind='BUSINESS', company_name=found['company_name'] or found['name'], email=found['email'],
            phone=found['phone'], warehouse=found['address'])
        created = directory.save_shipper(db, actor(organization_id), data, _key(*key_parts, 'shipper'))
        row = db.get(Shipper, UUID(str(created['id'])))
    if link is None: db.add(TmsLink(organization_id=organization_id, kind='SHIPPER', external_id=found['id'], record_id=row.id))
    return row


def _settle(organization_id, inbox_id, mapped):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = db.scalar(select(TmsInbox).where(TmsInbox.organization_id == organization_id, TmsInbox.id == inbox_id).with_for_update())
        if row is None or row.status != 'RECEIVED': return
        who, external = actor(organization_id), mapped['external']
        link = _link(db, organization_id, 'ORDER', external)
        order = db.get(Order, link.record_id) if link else None
        key_parts = (organization_id, external, row.revision)
        def review(reasons):
            row.status, row.missing, row.error_code = 'NEEDS_REVIEW', reasons[:50], None
        # Only the latest payload for a TMS order is applied, so a late or retried older one never rolls it back.
        newer = db.scalar(select(TmsInbox.id).where(TmsInbox.organization_id == organization_id, TmsInbox.external_id == row.external_id,
            TmsInbox.id != row.id, TmsInbox.status != 'IGNORED',
            (TmsInbox.received_at > row.received_at) | ((TmsInbox.received_at == row.received_at) & (TmsInbox.created_at > row.created_at))).limit(1))
        if newer:
            row.status, row.version = 'IGNORED', row.version + 1
            return
        try:
            with db.begin_nested():
                if mapped['cancelled']:
                    if order is None: row.status = 'IGNORED'
                    elif order.status == 'CANCELLED': row.status, row.order_id = 'IGNORED', order.id
                    elif order.status != 'NEW': review(['The TMS cancelled an order that is already assigned or in progress'])
                    else:
                        orders.cancel_order(db, who, order.id, Version(version=order.version), _key(*key_parts, 'cancel'))
                        row.status, row.order_id = 'ORDER_CANCELLED', order.id
                elif mapped['missing']: review(mapped['missing'])
                elif order is not None and order.status != 'NEW':
                    review([f'Order {order.number} is already {order.status.replace("_", " ").lower()}; apply TMS changes manually'])
                else:
                    shipper = _shipper(db, organization_id, mapped['shipper'], key_parts)
                    if shipper.status != 'ACTIVE': raise HTTPException(409, 'Shipper account is on hold or inactive.')
                    data = Booking.model_validate({**mapped['booking'], 'shipper_id': str(shipper.id)})
                    if order is None:
                        created = orders.create_order(db, who, data, _key(*key_parts, 'create'), source='IMPORT')
                        row.status, row.order_id = 'ORDER_CREATED', UUID(str(created['id']))
                        db.add(TmsLink(organization_id=organization_id, kind='ORDER', external_id=external, record_id=row.order_id))
                    else:
                        orders.update_order(db, who, order.id, OrderUpdate(version=order.version, booking=data), _key(*key_parts, 'update'))
                        row.status, row.order_id = 'ORDER_UPDATED', order.id
                    row.missing = []
        except HTTPException as error: review([f'Order could not be saved: {error.detail}'])
        except ValidationError as error: review([f"Order could not be saved: {error.errors()[0]['msg']}"])
        row.version += 1


def process(organization_id: UUID, inbox_id: UUID):
    claimed = _claim(organization_id, inbox_id)
    if claimed is None: return
    try: mapped = build(claimed['payload'], claimed['ctx'])
    except Exception as error:
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            row = db.get(TmsInbox, inbox_id)
            if row and row.status == 'RECEIVED':
                row.error_code = type(error).__name__[:80]
                if row.attempts >= MAX_ATTEMPTS: row.status = 'FAILED'
        return
    _settle(organization_id, inbox_id, mapped)


def pending_companies():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        return db.scalars(select(TmsInbox.organization_id).distinct().where(TmsInbox.status == 'RECEIVED', TmsInbox.attempts < MAX_ATTEMPTS)).all()


def process_pending(organization_id: UUID, limit=50) -> int:
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        identities = db.scalars(select(TmsInbox.id).where(TmsInbox.organization_id == organization_id, TmsInbox.status == 'RECEIVED',
            TmsInbox.attempts < MAX_ATTEMPTS).order_by(TmsInbox.received_at, TmsInbox.created_at).limit(limit)).all()
    for identity in identities: process(organization_id, identity)
    return len(identities)


def poll(organization_id: UUID) -> int | None:
    """Read the orders endpoint once and store new or changed orders. Returns the number stored, or None when not polling."""
    from app.intake import vault
    from . import client
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == organization_id))
        if row is None or not row.enabled or row.sync_method != 'polling': return None
        base, path, mapping = row.base_url, row.orders_endpoint, row.field_maps.get('order', {})
        auth = client.headers(row.auth_type, vault.unseal(row.credential, 'TMS API key'))
    try: rows, error = client.fetch_records(base, path, auth), None
    except client.TmsError as failure: rows, error = [], failure.code
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        stored = store(db, organization_id, rows, mapping, 'POLLING')[0] if rows else 0
        row = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == organization_id).with_for_update())
        if row: row.last_order_poll_at, row.last_error = now(), error
    return stored
