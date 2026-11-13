"""What dispatcher chat may change. Each entry is an existing workspace command with its request schema, so a confirmed
chat change runs the same authorization, validation, idempotency, audit and events as the workspace form. Records outside
this table (dispatcher accounts, passwords, company billing, integrations, mailboxes) cannot be reached from chat."""
from dataclasses import dataclass
from typing import Callable
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from app.operations import orders, directory, archive, dispatch, issues
from app.operations.common import settings
from app.operations.models import Order, Shipper, Driver, Vehicle, Catalog, RateCard, Issue
from app.operations.schemas import (Booking, OrderUpdate, Version, RouteCommand, IssueResolve, ShipperData, ShipperUpdate,
    DriverData, DriverUpdate, VehicleData, VehicleUpdate, CatalogCreate, CatalogUpdate, RateCreate, RateUpdate, SettingsUpdate)
from .search import pattern

CHOICES = 5
CATALOG = {'SERVICE': 'service level', 'ACCESSORIAL': 'additional service', 'VEHICLE_TYPE': 'vehicle type', 'PACKAGE_TYPE': 'package type'}
NOUNS = {'ORDER': 'order', 'SHIPPER': 'shipper', 'DRIVER': 'driver', 'VEHICLE': 'vehicle', 'ISSUE': 'issue',
    'RATE_CARD': 'rate card', 'SETTINGS': 'company settings', **CATALOG}


@dataclass(frozen=True)
class Operation:
    verb: str
    body: type[BaseModel]
    run: Callable  # (db, actor, target_id, body, key) -> workspace response
    drafted: bool = False  # the dispatcher supplies field values, so the AI drafts the body
    destructive: bool = False


def _directory(save, archive_command, create, update):
    return {'CREATE': Operation('Add', create, lambda db, a, t, b, k: save(db, a, b, k), True),
        'UPDATE': Operation('Edit', update, lambda db, a, t, b, k: save(db, a, b, k, t), True),
        'DELETE': Operation('Archive', Version, archive_command, destructive=True)}


OPERATIONS = {
    'ORDER': {'CREATE': Operation('Create', Booking, lambda db, a, t, b, k: orders.create_order(db, a, b, k), True),
        'UPDATE': Operation('Edit', OrderUpdate, orders.update_order, True),
        'CANCEL': Operation('Cancel', Version, orders.cancel_order, destructive=True),
        'COMPLETE': Operation('Complete', Version, orders.complete_order, destructive=True),
        'UNASSIGN': Operation('Unassign', RouteCommand, dispatch.release)},
    'SHIPPER': _directory(directory.save_shipper, archive.archive_shipper, ShipperData, ShipperUpdate),
    'DRIVER': _directory(directory.save_driver, archive.archive_driver, DriverData, DriverUpdate),
    'VEHICLE': _directory(directory.save_vehicle, archive.archive_vehicle, VehicleData, VehicleUpdate),
    'ISSUE': {'RESOLVE': Operation('Resolve', IssueResolve, issues.resolve, True)},
    **{kind: {'CREATE': Operation('Add', CatalogCreate, lambda db, a, t, b, k: directory.save_catalog(db, a, b, k), True),
        'UPDATE': Operation('Edit', CatalogUpdate, lambda db, a, t, b, k: directory.save_catalog(db, a, b, k, t), True),
        'DELETE': Operation('Delete', Version, directory.remove_catalog, destructive=True)} for kind in CATALOG},
    'RATE_CARD': {'CREATE': Operation('Add', RateCreate, lambda db, a, t, b, k: directory.save_rate(db, a, b, k), True),
        'UPDATE': Operation('Edit', RateUpdate, lambda db, a, t, b, k: directory.save_rate(db, a, b, k, t), True),
        'DELETE': Operation('Archive', Version, archive.archive_rate, destructive=True)},
    'SETTINGS': {'UPDATE': Operation('Edit', SettingsUpdate, lambda db, a, t, b, k: directory.save_settings(db, a, b, k), True)},
}
# Orders are cancelled, never deleted.
ALIASES = {('ORDER', 'DELETE'): 'CANCEL'}


def operation(entity, verb):
    """`(name, Operation)` for an allowed pair such as ('DRIVER', 'UPDATE'), else None."""
    verb = ALIASES.get((entity, verb), verb)
    found = OPERATIONS.get(entity, {}).get(verb)
    return (f'{entity}.{verb}', found) if found else None


def named(name):
    entity, _, verb = name.partition('.')
    found = OPERATIONS.get(entity, {}).get(verb)
    return (entity, found) if found and ALIASES.get((entity, verb)) is None else (None, None)


def label(entity, row):
    """Plain name of a record for chat text and change lines."""
    if entity == 'ORDER': return row.number
    if entity == 'SHIPPER': return row.company_name or row.name
    if entity == 'DRIVER': return row.name
    if entity == 'VEHICLE':
        name = row.data.get('name') or row.number
        return f'{name} ({row.plate})' if row.plate and row.plate != name else name
    if entity in CATALOG or entity == 'RATE_CARD': return row.data.get('name') or row.code
    return str(row.id)


def query(entity, org):
    """Tenant-filtered live records of one entity and the fields a dispatcher may name them by."""
    if entity == 'ORDER': return select(Order).where(Order.organization_id == org), [Order.number]
    if entity == 'SHIPPER':
        return (select(Shipper).where(Shipper.organization_id == org, Shipper.archived_at.is_(None), Shipper.warehouse.is_not(None)),
            [Shipper.name, Shipper.company_name, Shipper.number, Shipper.email])
    if entity == 'DRIVER':
        return select(Driver).where(Driver.organization_id == org, Driver.archived_at.is_(None)), [Driver.name, Driver.number, Driver.email]
    if entity == 'VEHICLE':
        return (select(Vehicle).where(Vehicle.organization_id == org, Vehicle.archived_at.is_(None)),
            [Vehicle.number, Vehicle.plate, Vehicle.data['name'].astext])
    if entity in CATALOG:
        return (select(Catalog).where(Catalog.organization_id == org, Catalog.kind == entity, Catalog.active.is_(True)),
            [Catalog.code, Catalog.data['name'].astext])
    if entity == 'RATE_CARD':
        return select(RateCard).where(RateCard.organization_id == org, RateCard.active.is_(True)), [RateCard.code, RateCard.data['name'].astext]
    raise ValueError(entity)


def find(db, org, entity, term):
    """Exact (case-insensitive) matches first; otherwise up to CHOICES partial matches, all inside the company."""
    base, fields = query(entity, org)
    model = base.column_descriptions[0]['entity']
    exact = db.scalars(base.where(or_(*[func.lower(f) == term.lower() for f in fields])).limit(CHOICES)).all()
    if exact: return exact
    return db.scalars(base.where(or_(*[f.ilike(pattern(term), escape='\\') for f in fields]))
        .order_by(model.updated_at.desc(), model.id).limit(CHOICES)).all()


def by_id(db, org, entity, identity):
    base, _ = query(entity, org)
    return db.scalar(base.where(base.column_descriptions[0]['entity'].id == identity))


def open_issues(db, org, order):
    return db.scalars(select(Issue).where(Issue.organization_id == org, Issue.order_id == order.id, Issue.resolved.is_(False))
        .order_by(Issue.created_at.desc())).all()


def editable(db, actor, entity, row):
    """The current record shaped as its workspace update request, without `version`."""
    if entity == 'ORDER': return {'booking': row.facts}
    if entity == 'SHIPPER':
        view = directory.shipper_view(db, actor, row)
        return {'data': {field: view[field] for field in ShipperData.model_fields if field in view}, 'status': view['status']}
    if entity == 'DRIVER':
        data = {**row.data, 'name': row.name, 'email': row.email, 'phone': row.phone, 'address': row.address,
            'vehicle_id': str(row.vehicle_id) if row.vehicle_id else None, 'active': row.active}
        return {'data': {field: data[field] for field in DriverData.model_fields if field in data}}
    if entity == 'VEHICLE': return {'data': {field: row.data[field] for field in VehicleData.model_fields if field in row.data}}
    if entity in CATALOG: return {'data': row.data}
    if entity == 'RATE_CARD': return {'is_default': row.is_default, 'active': row.active, 'data': row.data}
    if entity == 'SETTINGS': return {'data': settings(db, actor)[0].data}
    raise ValueError(entity)
