"""What dispatcher chat may change. Each entry is an existing workspace command with its request schema, so a confirmed
chat change runs the same authorization, validation, idempotency, audit and events as the workspace form. Records outside
this table (dispatcher accounts, company billing) cannot be reached from chat. Deleting an order, shipper, driver or vehicle,
even "permanently", archives it so its history stays. New passwords are generated on confirmation; integration secrets never pass through chat."""
import importlib
import re
from dataclasses import dataclass
from typing import Callable
from pydantic import BaseModel
from app.events.routes import mark_all_read, delete_all, inbox
from sqlalchemy import select, func, or_
from app.operations import orders, directory, archive, dispatch, issues
from app.operations.common import settings
from app.operations.models import Order, Shipper, Driver, Vehicle, Catalog, RateCard, Issue
from app.operations.schemas import (Booking, OrderUpdate, Version, RouteCommand, IssueResolve, IssueBatch, ShipperData, ShipperUpdate,
    DriverData, DriverUpdate, VehicleData, VehicleUpdate, CatalogCreate, CatalogUpdate, RateCreate, RateUpdate, SettingsUpdate)
from app.intake.models import EmailIntake, MailboxConnection
from app.intake.schemas import IntakeVersion, IntakeShipper, IntakeOrder, MailboxInput
from app.integrations.models import TmsInbox, TmsConnection
from app.integrations.schemas import TmsInput
from app.operations.schemas import QuoteSend
from .schemas import NoFields, AlertClear, RequestBatch, ConnectorChange, OrderBatchUpdate, OrderBatchClose
from .search import pattern

CHOICES = 5
BATCH = 200  # most issues one chat confirmation resolves; IssueBatch accepts no more
CATALOG = {'SERVICE': 'service level', 'ACCESSORIAL': 'additional service', 'VEHICLE_TYPE': 'vehicle type', 'PACKAGE_TYPE': 'package type'}
NOUNS = {'ORDER': 'order', 'SHIPPER': 'shipper', 'DRIVER': 'driver', 'VEHICLE': 'vehicle', 'ISSUE': 'issue',
    'RATE_CARD': 'rate card', 'SETTINGS': 'company settings', 'NOTIFICATION': 'notification', 'ALERT': 'alert',
    'REQUEST': 'incoming request', 'EMAIL_REQUEST': 'email request', 'TMS_REQUEST': 'TMS order', 'QUOTE': 'quote',
    'TMS': 'TMS connection', 'MAILBOX': 'order mailbox', 'CONNECTOR': 'connector', **CATALOG}
# Requests that are still waiting for the dispatcher (the review queue).
EMAIL_OPEN = ('NEEDS_REVIEW', 'UNKNOWN_SENDER', 'FAILED', 'NOT_AN_ORDER')
TMS_OPEN = ('NEEDS_REVIEW', 'FAILED')


@dataclass(frozen=True)
class Operation:
    verb: str
    body: type[BaseModel]
    run: Callable  # (db, actor, target_id, body, key) -> workspace response
    drafted: bool = False  # the dispatcher supplies field values, so the AI drafts the body
    destructive: bool = False
    title: str = ''  # card title when "<verb> <noun>" does not read well
    whole: bool = False  # acts on the company or the dispatcher's own records: no target record
    past: str = ''  # "is <past>" in the confirmation reply when the verb's own past tense does not fit


def later(module, name, *names):
    """Runner calling `module.name` (imported on first use, avoiding import cycles) with the arguments it names."""
    def run(db, actor, target, body, key):
        values = {'db': db, 'actor': actor, 'target': target, 'body': body, 'key': key, 'slug': None}
        return getattr(importlib.import_module(module), name)(*[values[n] for n in names])
    return run


def _directory(save, archive_command, create, update):
    return {'CREATE': Operation('Add', create, lambda db, a, t, b, k: save(db, a, b, k), True),
        'UPDATE': Operation('Edit', update, lambda db, a, t, b, k: save(db, a, b, k, t), True),
        'DELETE': Operation('Archive', Version, archive_command, destructive=True)}


OPERATIONS = {
    'ORDER': {'CREATE': Operation('Create', Booking, lambda db, a, t, b, k: orders.create_order(db, a, b, k), True),
        'UPDATE': Operation('Edit', OrderUpdate, orders.update_order, True),
        'UPDATE_MANY': Operation('Edit', OrderBatchUpdate, later('app.assistant.changes', 'run_order_batch_update', 'db', 'actor', 'body', 'key'),
            title='Edit orders', whole=True, past='updated'),
        'CANCEL': Operation('Cancel', Version, orders.cancel_order, destructive=True),
        'CANCEL_MANY': Operation('Cancel', OrderBatchClose, later('app.assistant.changes', 'run_order_batch_cancel', 'db', 'actor', 'body', 'key'),
            destructive=True, title='Cancel orders', whole=True, past='cancelled'),
        'ARCHIVE': Operation('Archive', Version, orders.archive_order, destructive=True),
        'ARCHIVE_MANY': Operation('Archive', OrderBatchClose, later('app.assistant.changes', 'run_order_batch_archive', 'db', 'actor', 'body', 'key'),
            destructive=True, title='Archive orders', whole=True, past='archived'),
        'RESTORE': Operation('Restore', Version, orders.restore_order),
        'RESTORE_MANY': Operation('Restore', OrderBatchClose, later('app.assistant.changes', 'run_order_batch_restore', 'db', 'actor', 'body', 'key'),
            title='Restore orders', whole=True, past='restored'),
        'COMPLETE': Operation('Complete', Version, orders.complete_order, destructive=True),
        'COMPLETE_MANY': Operation('Complete', OrderBatchClose, later('app.assistant.changes', 'run_order_batch_complete', 'db', 'actor', 'body', 'key'),
            destructive=True, title='Complete orders', whole=True, past='completed'),
        'UNASSIGN': Operation('Unassign', RouteCommand, dispatch.release)},
    'SHIPPER': {**_directory(directory.save_shipper, archive.archive_shipper, ShipperData, ShipperUpdate),
        'RESET_PASSWORD': Operation('Reset password for', Version, later('app.assistant.workspace', 'reset_shipper', 'db', 'actor', 'target', 'body', 'key'),
            past='given a new password')},
    'DRIVER': {**_directory(directory.save_driver, archive.archive_driver, DriverData, DriverUpdate),
        'RESET_PASSWORD': Operation('Reset password for', Version, later('app.assistant.workspace', 'reset_driver', 'db', 'actor', 'target', 'key'),
            past='given a new password')},
    'VEHICLE': _directory(directory.save_vehicle, archive.archive_vehicle, VehicleData, VehicleUpdate),
    'ISSUE': {'RESOLVE': Operation('Resolve', IssueResolve, issues.resolve, True),
        'RESOLVE_ALL': Operation('Resolve', IssueBatch, lambda db, a, t, b, k: issues.resolve_many(db, a, b, k), title='Resolve open issues', whole=True)},
    'NOTIFICATION': {'READ_ALL': Operation('Mark read', NoFields, lambda db, a, t, b, k: mark_all_read(db, a, k), title='Mark all notifications read', whole=True),
        'DELETE_ALL': Operation('Delete', NoFields, lambda db, a, t, b, k: delete_all(db, a, k), destructive=True, title='Delete all notifications', whole=True)},
    'ALERT': {'CLEAR_ALL': Operation('Clear', AlertClear, later('app.assistant.cleanup', 'run_clear', 'db', 'actor', 'body', 'key'),
        title='Clear Needs Attention', whole=True, past='cleared')},
    'REQUEST': {'DISCARD_ALL': Operation('Discard', RequestBatch, later('app.assistant.workspace', 'run_discard_all', 'db', 'actor', 'body', 'key'),
        destructive=True, title='Discard incoming requests', whole=True, past='discarded')},
    'EMAIL_REQUEST': {
        'DISCARD': Operation('Discard', IntakeVersion, later('app.intake.routes', 'discard', 'slug', 'target', 'body', 'db', 'key', 'actor'), destructive=True),
        'RETRY': Operation('Read again', IntakeVersion, later('app.intake.routes', 'retry', 'slug', 'target', 'body', 'db', 'key', 'actor'),
            past='sent back to the Order agent'),
        'LINK_SHIPPER': Operation('Link shipper to', IntakeShipper, later('app.intake.routes', 'link_shipper', 'slug', 'target', 'body', 'db', 'key', 'actor'),
            past='linked and being read again'),
        'CREATE_ORDER': Operation('Create order from', IntakeOrder, later('app.intake.routes', 'create_order', 'slug', 'target', 'body', 'db', 'key', 'actor'),
            past='turned into an order')},
    'TMS_REQUEST': {
        'DISCARD': Operation('Discard', NoFields, later('app.integrations.routes', 'dismiss_inbox', 'slug', 'target', 'db', 'key', 'actor'), destructive=True),
        'RETRY': Operation('Read again', NoFields, later('app.integrations.routes', 'retry_inbox', 'slug', 'target', 'db', 'key', 'actor'), past='sent for another read')},
    'QUOTE': {'CREATE': Operation('Create', Booking, later('app.operations.quotes', 'create_quote', 'db', 'actor', 'body', 'key'), True),
        'SEND': Operation('Send', QuoteSend, later('app.operations.mail', 'send_quote', 'db', 'actor', 'target', 'body', 'key'), past='on its way')},
    'TMS': {'UPDATE': Operation('Edit', TmsInput, later('app.integrations.routes', 'save_tms', 'slug', 'body', 'db', 'key', 'actor'), True, whole=True),
        'SYNC': Operation('Sync', NoFields, later('app.integrations.routes', 'sync_fleet', 'slug', 'db', 'actor'), title='Sync drivers and vehicles from the TMS',
            whole=True, past='synced')},
    'MAILBOX': {'UPDATE': Operation('Edit', MailboxInput, later('app.intake.routes', 'save_mailbox', 'slug', 'body', 'db', 'key', 'actor'), True, whole=True)},
    'CONNECTOR': {'SYNC': Operation('Sync', ConnectorChange, later('app.assistant.workspace', 'connector_sync', 'db', 'actor', 'body', 'key'), whole=True,
            past='syncing'),
        'DISCONNECT': Operation('Disconnect', ConnectorChange, later('app.assistant.workspace', 'connector_disconnect', 'db', 'actor', 'body', 'key'),
            destructive=True, whole=True)},
    **{kind: {'CREATE': Operation('Add', CatalogCreate, lambda db, a, t, b, k: directory.save_catalog(db, a, b, k), True),
        'UPDATE': Operation('Edit', CatalogUpdate, lambda db, a, t, b, k: directory.save_catalog(db, a, b, k, t), True),
        'DELETE': Operation('Delete', Version, directory.remove_catalog, destructive=True)} for kind in CATALOG},
    'RATE_CARD': {'CREATE': Operation('Add', RateCreate, lambda db, a, t, b, k: directory.save_rate(db, a, b, k), True),
        'UPDATE': Operation('Edit', RateUpdate, lambda db, a, t, b, k: directory.save_rate(db, a, b, k, t), True),
        'DELETE': Operation('Archive', Version, archive.archive_rate, destructive=True)},
    'SETTINGS': {'UPDATE': Operation('Edit', SettingsUpdate, lambda db, a, t, b, k: directory.save_settings(db, a, b, k), True, whole=True)},
}
# Orders are archived, never erased: an unassigned one is cancelled first.
ALIASES = {('ORDER', 'DELETE'): 'ARCHIVE'}


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


def query(entity, org, archived=False):
    """Tenant-filtered live records of one entity (archived Orders on request) and the fields a dispatcher may name them by."""
    if entity == 'ORDER':
        return select(Order).where(Order.organization_id == org, Order.archived_at.is_not(None) if archived else Order.archived_at.is_(None)), [Order.number]
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


def clean_order_term(term):
    """Normalize '#123' or 'order #123' to '123' while preserving literal codes like 'ORD-123'."""
    cleaned = re.sub(r'^(?:order\s*)?#+\s*', '', term.strip(), flags=re.I)
    cleaned = re.sub(r'^order\s+(?=[a-z0-9-])', '', cleaned, flags=re.I).strip()
    return cleaned or term.strip()


def find(db, org, entity, term, archived=False):
    """Exact (case-insensitive) matches first; otherwise up to CHOICES partial matches, all inside the company."""
    if entity == 'ORDER': term = clean_order_term(term)
    base, fields = query(entity, org, archived)
    model = base.column_descriptions[0]['entity']
    exact = db.scalars(base.where(or_(*[func.lower(f) == term.lower() for f in fields])).limit(CHOICES)).all()
    if exact: return exact
    return db.scalars(base.where(or_(*[f.ilike(pattern(term), escape='\\') for f in fields]))
        .order_by(model.updated_at.desc(), model.id).limit(CHOICES)).all()


def by_id(db, org, entity, identity):
    base, _ = query(entity, org)
    return db.scalar(base.where(base.column_descriptions[0]['entity'].id == identity))


def open_issues(db, org, order=None):
    """Unresolved reported issues, newest first: one Order's, or the company's (at most BATCH)."""
    query = select(Issue).where(Issue.organization_id == org, Issue.resolved.is_(False))
    if order is not None: query = query.where(Issue.order_id == order.id)
    return db.scalars(query.order_by(Issue.created_at.desc(), Issue.id).limit(BATCH)).all()


def company_record(db, org, entity):
    """The company's single TMS connection or order mailbox, or None before it is set up in Integrations."""
    model = TmsConnection if entity == 'TMS' else MailboxConnection
    return db.scalar(select(model).where(model.organization_id == org))


def notifications(actor):
    return inbox(actor)


def open_requests(db, org, limit=200):
    """E-mails and TMS orders waiting for the dispatcher, newest first."""
    emails = db.scalars(select(EmailIntake).where(EmailIntake.organization_id == org, EmailIntake.status.in_(EMAIL_OPEN))
        .order_by(EmailIntake.received_at.desc()).limit(limit)).all()
    orders = db.scalars(select(TmsInbox).where(TmsInbox.organization_id == org, TmsInbox.status.in_(TMS_OPEN))
        .order_by(TmsInbox.received_at.desc()).limit(limit)).all()
    return emails, orders


def waiting_requests(db, org):
    return sum(db.scalar(select(func.count()).select_from(model).where(model.organization_id == org, model.status.in_(statuses)))
        for model, statuses in ((EmailIntake, EMAIL_OPEN), (TmsInbox, TMS_OPEN)))


def find_requests(db, org, term):
    """Waiting requests matching a sender, subject or TMS order number: [('EMAIL' | 'TMS', row)], at most CHOICES."""
    like = pattern(term)
    emails = db.scalars(select(EmailIntake).where(EmailIntake.organization_id == org, EmailIntake.status.in_(EMAIL_OPEN),
        or_(EmailIntake.from_address.ilike(like, escape='\\'), EmailIntake.from_name.ilike(like, escape='\\'),
            EmailIntake.subject.ilike(like, escape='\\'))).order_by(EmailIntake.received_at.desc()).limit(CHOICES)).all()
    orders = db.scalars(select(TmsInbox).where(TmsInbox.organization_id == org, TmsInbox.status.in_(TMS_OPEN),
        TmsInbox.external_id.ilike(like, escape='\\')).order_by(TmsInbox.received_at.desc()).limit(CHOICES)).all()
    return ([('EMAIL', row) for row in emails] + [('TMS', row) for row in orders])[:CHOICES]


def editable(db, actor, entity, row):
    """The current record shaped as its workspace update request, without `version`."""
    if entity == 'ORDER': return {'booking': row.facts}
    if entity == 'SHIPPER':
        view = directory.shipper_view(db, actor, row)
        # The ordered `rate_card_ids` list replaces the legacy single `rate_card_id` in chat edits.
        return {'data': {field: view[field] for field in ShipperData.model_fields if field in view and field != 'rate_card_id'}, 'status': view['status']}
    if entity == 'DRIVER':
        data = {**row.data, 'name': row.name, 'email': row.email, 'phone': row.phone, 'address': row.address,
            'vehicle_id': str(row.vehicle_id) if row.vehicle_id else None, 'active': row.active}
        return {'data': {field: data[field] for field in DriverData.model_fields if field in data}}
    if entity == 'VEHICLE': return {'data': {field: row.data[field] for field in VehicleData.model_fields if field in row.data}}
    if entity in CATALOG: return {'data': row.data}
    if entity == 'RATE_CARD': return {'is_default': row.is_default, 'active': row.active, 'data': row.data}
    if entity == 'SETTINGS': return {'data': settings(db, actor)[0].data}
    if entity == 'TMS':
        return {field: getattr(row, field) for field in TmsInput.model_fields if field not in {'credential', 'webhook_secret', 'version'}}
    if entity == 'MAILBOX':
        return {field: getattr(row, field) for field in MailboxInput.model_fields if field not in {'password', 'version'}}
    raise ValueError(entity)
