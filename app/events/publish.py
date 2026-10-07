"""Turn audited mutations into company events and notifications inside the same transaction.

`record` (called by `services.audit`) only queues the action. The `before_commit` hook resolves
each entity's final state, its audience and notification rules, then inserts the rows before the
transaction commits; a rollback discards the queue. Resolution runs under the company tenant
scope so a Shipper or driver action can address dispatchers; read endpoints re-apply the
actor's own audience, and RLS narrows Shipper/driver reads independently.
"""
from dataclasses import dataclass
from uuid import UUID, uuid4
from sqlalchemy import event as sa_event, text
from sqlalchemy.orm import Session
from app.database import context
from .models import Event, Notification

PENDING = 'dispatra_events'
# Account, credential and platform administration actions are audited but never fanned out.
PRIVATE = ('session.', 'password.', 'dispatcher.', 'platform_owner.', 'demo.', 'company.',
    'organization.created', 'organization.activated', 'organization.suspended')
ACTOR_TYPES = {'USER', 'SYSTEM', 'AGENT'}


@dataclass(frozen=True)
class Actor:
    id: UUID
    role: str | None
    kind: str


@dataclass
class Audience:
    entity: str
    entity_version: int | None = None
    order_id: UUID | None = None
    route_id: UUID | None = None
    dispatchers: bool = True
    shipper_id: UUID | None = None
    driver_id: UUID | None = None
    user_id: UUID | None = None


def record(db, actor, action, entity_id, organization_id, actor_type='USER'):
    if organization_id is None or action.startswith(PRIVATE) or action.endswith('.password_reset'): return
    if actor_type not in ACTOR_TYPES: raise ValueError('Unknown actor type.')
    queue = db.info.setdefault(PENDING, {})
    # One event per entity and action per transaction; later calls carry the newer version.
    queue.pop((action, entity_id), None)
    # Payment and platform helpers pass lightweight principals rather than `User` rows.
    role = getattr(actor, 'role', None) or ('SHIPPER' if getattr(actor, 'shipper_id', None) else None)
    queue[(action, entity_id)] = (Actor(actor.id, role, actor_type), organization_id)


def _order_scope(db, order, result):
    from app.operations.models import Route
    result.order_id, result.route_id, result.shipper_id = order.id, order.route_id, order.shipper_id
    route = db.get(Route, order.route_id) if order.route_id else None
    if route: result.driver_id = route.driver_id
    return result


def resolve(db, action, entity_id):
    """Audience and references from the entity's state at commit; missing rows fall back to dispatchers only."""
    from app.models import User
    from app.operations import models as m
    from app.intake.models import EmailIntake, MailboxConnection
    from app.dispatch.models import DispatchDecision
    kind = action.split('.', 1)[0]
    entity = {'customer': 'shipper', 'pricing': 'settings'}.get(kind, kind)
    model = {'order': m.Order, 'route': m.Route, 'stop': m.RouteStop, 'evidence': m.Evidence, 'issue': m.Issue,
        'invoice': m.Invoice, 'email': m.EmailDelivery, 'duty': m.DutySession, 'driver': m.Driver, 'shipper': m.Shipper,
        'vehicle': m.Vehicle, 'quote': m.Quote, 'rate': m.RateCard, 'catalog': m.Catalog, 'notification': Notification,
        'intake': EmailIntake, 'mailbox': MailboxConnection, 'dispatch': DispatchDecision}.get(entity)
    row = db.get(model, entity_id) if model else None
    result = Audience(entity, getattr(row, 'version', None))
    if row is None:
        if action == 'notification.read_all':
            user = db.get(User, entity_id)
            if user: result.dispatchers, result.user_id, result.shipper_id, result.driver_id = False, user.id, user.shipper_id, user.driver_id
        return result
    if entity == 'order': return _order_scope(db, row, result)
    if entity == 'route': result.route_id, result.driver_id = row.id, row.driver_id
    elif entity in {'stop', 'evidence'}:
        stop = db.get(m.OrderStop, row.stop_id)
        order = db.get(m.Order, stop.order_id) if stop else None
        if order: _order_scope(db, order, result)
        if entity == 'evidence': result.driver_id = row.driver_id
        else: result.route_id = row.route_id
    elif entity in {'issue', 'invoice'}:
        order = db.get(m.Order, row.order_id)
        if order: _order_scope(db, order, result)
        if entity == 'invoice': result.driver_id = None
    elif entity == 'duty': result.driver_id = row.driver_id
    # Dispatch decisions name the Order for dispatchers only; the Shipper and driver hear about the assignment itself.
    elif entity == 'dispatch': result.order_id = row.order_id
    elif entity == 'driver': result.driver_id = row.id
    elif entity == 'shipper': result.shipper_id = row.id
    elif entity == 'notification':
        result.dispatchers, result.user_id, result.shipper_id, result.driver_id = False, row.recipient_user_id, row.shipper_id, row.driver_id
        result.order_id, result.route_id = row.order_id, row.route_id
    return result


def _write(db, organization_id, items):
    from . import email, rules
    correlation = uuid4()
    recipients, mail = rules.Recipients(db, organization_id), email.Sender(db, organization_id)
    for (action, entity_id), actor in items:
        audience = resolve(db, action, entity_id)
        event = Event(id=uuid4(), organization_id=organization_id, type=action, actor_type=actor.kind, actor_id=actor.id,
            entity=audience.entity, entity_id=entity_id, entity_version=audience.entity_version, order_id=audience.order_id,
            route_id=audience.route_id, dispatchers=audience.dispatchers, shipper_id=audience.shipper_id,
            driver_id=audience.driver_id, user_id=audience.user_id, correlation_id=correlation)
        db.add(event)
        for note in rules.notes(db, action, entity_id, audience, actor, recipients):
            if actor.kind == 'USER' and note.user.id == actor.id: continue
            row = Notification(id=uuid4(), organization_id=organization_id, recipient_user_id=note.user.id,
                shipper_id=note.user.shipper_id, driver_id=note.user.driver_id, event_id=event.id, kind=action,
                severity=note.severity, order_id=audience.order_id, route_id=audience.route_id, title=note.title, body=note.body,
                dedupe_key=f'{action}:{entity_id}:{audience.entity_version or 0}')
            db.add(row)
            db.add(Event(id=uuid4(), organization_id=organization_id, type='notification.created', actor_type=actor.kind,
                actor_id=actor.id, entity='notification', entity_id=row.id, entity_version=1, order_id=row.order_id,
                route_id=row.route_id, dispatchers=False, shipper_id=row.shipper_id, driver_id=row.driver_id,
                user_id=row.recipient_user_id, correlation_id=correlation, causation_id=event.id))
            mail.queue(row, note.user)
        db.flush()


@sa_event.listens_for(Session, 'before_commit')
def _publish(db):
    queue = db.info.pop(PENDING, None)
    if not queue: return
    db.flush()
    saved = db.execute(text("SELECT current_setting('app.organization_id', true), current_setting('app.platform', true), "
        "current_setting('app.shipper_id', true), current_setting('app.driver_id', true)")).one()
    companies = {}
    for key, (actor, organization_id) in queue.items(): companies.setdefault(organization_id, []).append((key, actor))
    try:
        for organization_id, items in companies.items():
            context(db, organization_id)
            _write(db, organization_id, items)
    finally:
        context(db, saved[0] or None, saved[1] == 'true', saved[2] or None, saved[3] or None)


@sa_event.listens_for(Session, 'after_rollback')
def _discard(db):
    db.info.pop(PENDING, None)
