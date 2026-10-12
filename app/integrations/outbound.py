"""Order events out of Dispatra: status updates to the connected TMS and the company's signed API webhook.

Each target is a server consumer of the company event log. Events are read in commit order, sent one by one outside
any transaction, and the position advances only past events that were delivered or do not apply, so a failure is
retried from the same event and nothing is skipped or reordered. The event ID is sent as the idempotency key.
"""
import hashlib
import hmac
import json
import urllib.parse
from dataclasses import dataclass
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.events import feed
from app.intake import vault
from app.models import now
from app.operations.models import Driver, Order, OrderStop, Route, RouteStop, Vehicle, Evidence
from . import client
from .models import ApiWebhook, TmsConnection, TmsLink

ORDER_EVENTS = ('order.created', 'order.updated', 'order.cancelled', 'order.assigned', 'order.unassigned', 'order.started',
    'stop.arrived', 'stop.completed', 'evidence.accepted', 'issue.reported', 'issue.resolved', 'order.completion_held', 'order.completed')
BATCH = 50


def _tms_id(db, organization_id, kind, record_id):
    if record_id is None: return None
    return db.scalar(select(TmsLink.external_id).where(TmsLink.organization_id == organization_id, TmsLink.kind == kind, TmsLink.record_id == record_id))


def _stop(db, event, order):
    """Position (1-based, in booking order) and kind of the stop an event refers to, with who completed it."""
    stop_id, visit = None, None
    if event['entity'] == 'stop':
        visit = db.get(RouteStop, event['entity_id'])
        stop_id = visit.stop_id if visit else None
    elif event['entity'] == 'evidence':
        evidence = db.get(Evidence, event['entity_id'])
        stop_id = evidence.stop_id if evidence else None
    if stop_id is None: return None
    stop = db.get(OrderStop, stop_id)
    ids = [s['id'] for s in order.facts.get('stops', [])]
    result = {'index': ids.index(str(stop_id)) + 1 if str(stop_id) in ids else None, 'kind': stop.kind if stop else None}
    if event['type'] == 'stop.completed' and visit: result['completed_by'] = 'dispatcher' if (visit.movements or {}).get('completed_by') == 'DISPATCHER' else 'driver'
    return result


def payload(db, organization_id: UUID, event, external_order_id: str | None) -> dict | None:
    order = db.get(Order, event['order_id']) if event['order_id'] else None
    if order is None: return None
    route = db.get(Route, order.route_id) if order.route_id else None
    driver = db.get(Driver, route.driver_id) if route else None
    vehicle = db.get(Vehicle, route.vehicle_id) if route else None
    return {'event': event['type'], 'event_id': str(event['id']), 'occurred_at': event['created_at'].isoformat(),
        'order': {'id': str(order.id), 'number': order.number, 'status': order.status, 'reference': order.facts.get('external_reference') or None,
            **({'tms_id': external_order_id} if external_order_id else {})},
        'driver': {'name': driver.name, 'phone': driver.phone, 'tms_id': _tms_id(db, organization_id, 'DRIVER', driver.id)} if driver else None,
        'vehicle': {'unit': vehicle.data.get('unit_number'), 'plate': vehicle.plate, 'tms_id': _tms_id(db, organization_id, 'VEHICLE', vehicle.id)} if vehicle else None,
        'stop': _stop(db, event, order)}


@dataclass
class Target:
    consumer: str
    model: type
    tms: bool  # TMS status: only TMS-linked orders, sent to the status endpoint; API webhook: every order, signed

    def setup(self, db, organization_id):
        row = db.scalar(select(self.model).where(self.model.organization_id == organization_id))
        if row is None or not row.enabled: return None
        if self.tms: return {'auth': client.headers(row.auth_type, vault.unseal(row.credential, 'TMS API key')), 'base': row.base_url, 'path': row.status_endpoint}
        return {'url': row.url, 'secret': vault.unseal(row.secret, 'webhook signing secret')}

    def message(self, db, organization_id, event):
        if event['type'] not in ORDER_EVENTS or not event['order_id']: return None
        external = _tms_id(db, organization_id, 'ORDER', event['order_id'])
        if self.tms and external is None: return None
        return payload(db, organization_id, event, external)

    def send(self, config, body):
        extra = {'Idempotency-Key': body['event_id'], 'X-Dispatra-Event': body['event']}
        if self.tms:
            target = client.url(config['base'], config['path'].replace('{id}', urllib.parse.quote(body['order']['tms_id'], safe='')))
            return client.request('POST', target, {**config['auth'], **extra}, body)
        raw = json.dumps(body, separators=(',', ':'))
        signature = hmac.new(config['secret'].encode(), raw.encode(), hashlib.sha256).hexdigest()
        return client.request('POST', config['url'], {'Accept': 'application/json', 'User-Agent': 'Dispatra-Webhooks/1', 'X-Signature': signature, **extra}, body)

    def record(self, row, error):
        if self.tms: row.last_status_at, row.last_status_error = now(), error
        else: row.last_delivery_at, row.last_error = now(), error


TMS_STATUS = Target('tms-status', TmsConnection, True)
API_WEBHOOKS = Target('api-webhooks', ApiWebhook, False)


def deliver(organization_id: UUID, target: Target) -> int | None:
    """Send the next batch. Returns how many messages were sent, or None when the target is not configured."""
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        config = target.setup(db, organization_id)
        if config is None: return None
        rows, end = feed.consume(db, organization_id, target.consumer, BATCH)
        work = [(f"{row['tx']}-{row['seq']}", target.message(db, organization_id, row)) for row in rows]
    sent, reached, error = 0, None, None
    for position, body in work:
        if body is not None:
            try: target.send(config, body)
            except client.TmsError as failure:
                error = failure.code
                break
            sent += 1
        reached = position
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        if reached: feed.advance(db, organization_id, target.consumer, reached)
        row = db.scalar(select(target.model).where(target.model.organization_id == organization_id).with_for_update())
        if row and (sent or error): target.record(row, error)
    return sent


def companies(target: Target):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        return db.scalars(select(target.model.organization_id).where(target.model.enabled.is_(True))).all()
