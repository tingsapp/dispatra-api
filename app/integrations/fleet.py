"""Driver and vehicle sync from a company's TMS. Network calls run between short transactions, never inside one.

Writes go through the directory and archive use cases with a `SYSTEM` dispatcher actor, so validation, idempotency,
audit and events apply unchanged. Each record commits on its own; one bad record never blocks the rest.
"""
import hashlib
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.intake import booking, geocode, vault
from app.models import now
from app.operations import archive, directory
from app.operations.common import settings
from app.operations.models import Catalog, Driver, Vehicle
from app.operations.schemas import DriverData, DriverUpdate, VehicleData, VehicleUpdate, Version
from . import client
from .mapping import boolean, fingerprint, get, number, text
from .models import TmsConnection, TmsLink

MAX_ISSUES = 50
VEHICLE_SIZE = ('payload', 'length', 'width', 'height')


def actor(organization_id: UUID):
    return SimpleNamespace(id=None, role='DISPATCHER', organization_id=organization_id, shipper_id=None, driver_id=None, actor_type='SYSTEM')


def _key(organization_id, *parts) -> str:
    return hashlib.sha256(':'.join(map(str, (organization_id, *parts))).encode()).hexdigest()


class Skip(Exception):
    """A record that cannot be imported yet; the reason is shown to the dispatcher with the TMS ID when known."""
    def __init__(self, reason: str, external: str = ""):
        super().__init__(reason)
        self.external = external


def _invalid(error: ValidationError) -> str:
    first = error.errors()[0]
    return f"Invalid {'.'.join(map(str, first['loc'])) or 'value'}: {first['msg']}"


def _load(organization_id: UUID):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == organization_id))
        if row is None or not row.enabled: return None
        _, config = settings(db, actor(organization_id))
        types = [{'id': str(t.id), 'code': t.code, 'name': t.data.get('name', t.code), 'data': t.data} for t in db.scalars(select(Catalog).where(
            Catalog.organization_id == organization_id, Catalog.kind == 'VEHICLE_TYPE', Catalog.active.is_(True)))]
        return {'base': row.base_url, 'auth': client.headers(row.auth_type, vault.unseal(row.credential, 'TMS API key')),
            'drivers': row.drivers_endpoint, 'vehicles': row.vehicles_endpoint, 'maps': row.field_maps,
            'weight': Decimal('0.45359237') if config.weight_unit == 'lb' else Decimal('1'),
            'size': Decimal('2.54') if config.dimension_unit == 'in' else Decimal('1'), 'types': types}


def _link(db, organization_id, kind, external_id):
    return db.scalar(select(TmsLink).where(TmsLink.organization_id == organization_id, TmsLink.kind == kind, TmsLink.external_id == external_id).with_for_update())


def _measure(value, factor, fallback):
    amount = number(value)
    if amount is not None and amount > 0: return str((amount * factor).quantize(Decimal('0.01')))
    return fallback


def vehicle_data(record: dict, mapping: dict, setup: dict) -> tuple[str, dict]:
    external = text(get(record, mapping, 'id'), 200)
    if not external: raise Skip('TMS vehicle ID missing')
    kind = text(get(record, mapping, 'vehicle_type'))
    type_id = booking.pick(kind, setup['types'])
    if not type_id: raise Skip(f'Vehicle type {kind!r} does not match a Dispatra vehicle type' if kind else 'Vehicle type missing', external)
    defaults = next(t['data'] for t in setup['types'] if t['id'] == type_id)
    unit = text(get(record, mapping, 'unit_number'), 50)
    size = {key: _measure(get(record, mapping, key), setup['weight' if key == 'payload' else 'size'], defaults.get(f'{key}_kg' if key == 'payload' else f'{key}_cm'))
        for key in VEHICLE_SIZE}
    if not all(size.values()): raise Skip('Payload and size are missing in the TMS and the vehicle type', external)
    year = number(get(record, mapping, 'year'))
    pallets = number(get(record, mapping, 'pallet_capacity'))
    active = boolean(get(record, mapping, 'active'))
    province = text(get(record, mapping, 'province'), 30)
    data = {'name': text(get(record, mapping, 'name'), 160) or unit, 'unit_number': unit, 'type_id': type_id, 'plate': text(get(record, mapping, 'plate'), 30),
        'province': booking.province(province) or province.upper(), 'payload_kg': size['payload'], 'length_cm': size['length'], 'width_cm': size['width'],
        'height_cm': size['height'], 'pallet_capacity': int(pallets) if pallets is not None else int(defaults.get('pallet_capacity') or 0),
        'make_model': text(get(record, mapping, 'make_model'), 160), 'year': int(year) if year is not None else None,
        'vin': text(get(record, mapping, 'vin'), 50), 'active': True if active is None else active}
    try: return external, VehicleData.model_validate(data).model_dump(mode='json')
    except ValidationError as error: raise Skip(_invalid(error), external) from None


def driver_data(record: dict, mapping: dict) -> tuple[str, dict, str | None]:
    """(TMS ID, DriverData fields without coordinates, mapped truck unit or None when not mapped)."""
    external = text(get(record, mapping, 'id'), 200)
    if not external: raise Skip('TMS driver ID missing')
    street, city = text(get(record, mapping, 'address')), text(get(record, mapping, 'city'), 160)
    province, postal = booking.province(text(get(record, mapping, 'province'))), booking.postal(text(get(record, mapping, 'postal_code')))
    if not (street and city and province and postal): raise Skip('Home address is incomplete or not a Canadian address', external)
    full = street if postal.replace(' ', '') in street.upper().replace(' ', '') else f'{street}, {city}, {province} {postal}'
    active = boolean(get(record, mapping, 'active'))
    data = {'name': text(get(record, mapping, 'name'), 160), 'email': text(get(record, mapping, 'email'), 254).lower(), 'phone': text(get(record, mapping, 'phone'), 50),
        'address': {'text': full, 'city': city, 'province': province, 'postal_code': postal, 'country': 'CA'}, 'active': True if active is None else active}
    unit = text(get(record, mapping, 'vehicle_unit'), 50).upper() if mapping.get('vehicle_unit') else None
    try: DriverData.model_validate(data)
    except ValidationError as error: raise Skip(_invalid(error), external) from None
    return external, data, unit


def _unchanged(organization_id, kind, external, payload) -> bool:
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        link = db.scalar(select(TmsLink).where(TmsLink.organization_id == organization_id, TmsLink.kind == kind, TmsLink.external_id == external))
        return link is not None and link.fingerprint == fingerprint(payload)


def _save(organization_id, kind, external, payload, write):
    """Create or update one record under its own transaction; returns 'created' or 'updated'."""
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        link = _link(db, organization_id, kind, external)
        identity, created = write(db, link.record_id if link else None, _key(organization_id, kind, external, fingerprint(payload)))
        if link is None:
            link = TmsLink(organization_id=organization_id, kind=kind, external_id=external, record_id=identity)
            db.add(link)
        link.fingerprint = fingerprint(payload)
        return 'created' if created else 'updated'


def _write_vehicle(organization_id, data):
    def write(db, identity, key):
        who = actor(organization_id)
        if identity is None:
            identity = db.scalar(select(Vehicle.id).where(Vehicle.organization_id == organization_id, Vehicle.archived_at.is_(None),
                func.upper(func.btrim(Vehicle.data['unit_number'].astext)) == data['unit_number'].upper()))
        if identity is None: return UUID(str(directory.save_vehicle(db, who, VehicleData.model_validate(data), key)['id'])), True
        row = db.get(Vehicle, identity)
        directory.save_vehicle(db, who, VehicleUpdate(version=row.version, data=VehicleData.model_validate(data)), key, identity)
        return identity, False
    return write


def _write_driver(organization_id, data, unit):
    def write(db, identity, key):
        who = actor(organization_id)
        if identity is None:
            identity = db.scalar(select(Driver.id).where(Driver.organization_id == organization_id, func.lower(Driver.email) == data['email'], Driver.archived_at.is_(None)))
        row = db.get(Driver, identity) if identity else None
        vehicle_id = row.vehicle_id if row and unit is None else None
        if unit:
            vehicle_id = db.scalar(select(Vehicle.id).where(Vehicle.organization_id == organization_id, Vehicle.active.is_(True),
                func.upper(func.btrim(Vehicle.data['unit_number'].astext)) == unit))
            if vehicle_id is None: raise Skip(f'Truck unit {unit} was not found in Dispatra')
        # TMS-owned fields replace Dispatra's; shift, crew and qualifications stay as dispatchers set them.
        kept = {key: value for key, value in (row.data if row else {}).items() if key in DriverData.model_fields}
        payload = DriverData.model_validate({**kept, **data, 'vehicle_id': vehicle_id})
        if row is None: return UUID(str(directory.save_driver(db, who, payload, key)['id'])), True
        directory.save_driver(db, who, DriverUpdate(version=row.version, data=payload), key, identity)
        return identity, False
    return write


def _archive(organization_id, kind, seen: set[str], issues: list) -> int:
    archived = 0
    model, use = (Driver, archive.archive_driver) if kind == 'DRIVER' else (Vehicle, archive.archive_vehicle)
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        links = db.scalars(select(TmsLink).where(TmsLink.organization_id == organization_id, TmsLink.kind == kind)).all()
        gone = [(link.external_id, link.record_id) for link in links if link.external_id not in seen]
    for external, identity in gone:
        try:
            with Session(engine) as db, db.begin():
                context(db, organization_id)
                row = db.get(model, identity)
                if row is None or row.archived_at is not None: continue
                use(db, actor(organization_id), identity, Version(version=row.version), _key(organization_id, kind, external, 'archive', row.version))
                archived += 1
        except HTTPException as error: issues.append({'kind': kind.lower(), 'id': external, 'reason': f'Not archived: {error.detail}'})
    return archived


def _apply(organization_id, kind, rows, convert, issues):
    """`convert(record)` → (TMS ID, comparable payload, prepare); `prepare()` may call the network and returns the writer."""
    counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'skipped': 0, 'archived': 0}
    seen = set()
    for record in rows:
        external = None
        try:
            external, payload, prepare = convert(record)
            seen.add(external)
            if _unchanged(organization_id, kind, external, payload): counts['unchanged'] += 1
            else: counts[_save(organization_id, kind, external, payload, prepare())] += 1
        except (Skip, HTTPException, ValidationError) as error:
            counts['skipped'] += 1
            reason = error.detail if isinstance(error, HTTPException) else _invalid(error) if isinstance(error, ValidationError) else str(error)
            issues.append({'kind': kind.lower(), 'id': external or getattr(error, 'external', ''), 'reason': str(reason)})
    # An empty answer is treated as a TMS problem, never as "everyone left".
    if rows: counts['archived'] = _archive(organization_id, kind, seen, issues)
    return counts


def sync(organization_id: UUID) -> dict | None:
    """Import vehicles then drivers (drivers reference trucks). Returns the stored result, or None when not connected."""
    setup = _load(organization_id)
    if setup is None: return None
    maps, issues = setup['maps'], []
    result = {'drivers': None, 'vehicles': None, 'issues': issues, 'error': None}
    try:
        vehicles = client.fetch_records(setup['base'], setup['vehicles'], setup['auth'])
        drivers = client.fetch_records(setup['base'], setup['drivers'], setup['auth'])
    except client.TmsError as error:
        result['error'] = error.code
    else:
        vehicle_map, driver_map = maps.get('vehicle', {}), maps.get('driver', {})
        def vehicle(record):
            external, data = vehicle_data(record, vehicle_map, setup)
            return external, data, lambda: _write_vehicle(organization_id, data)
        def driver(record):
            external, data, unit = driver_data(record, driver_map)
            def prepare():
                # Geocode only new or changed drivers: the home point is the dispatch fallback position.
                located = geocode.verify(data['address']['text'], data['address']['postal_code'])
                address = {**data['address'], **({'latitude': located['latitude'], 'longitude': located['longitude']} if located else {})}
                return _write_driver(organization_id, {**data, 'address': address}, unit)
            return external, {**data, 'vehicle_unit': unit}, prepare
        result['vehicles'] = _apply(organization_id, 'VEHICLE', vehicles, vehicle, issues)
        result['drivers'] = _apply(organization_id, 'DRIVER', drivers, driver, issues)
    result['issues'] = issues[:MAX_ISSUES]
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == organization_id).with_for_update())
        if row:
            row.last_fleet_sync_at, row.last_fleet_result, row.last_error = now(), result, result['error']
    return result
