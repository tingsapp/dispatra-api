import hashlib
import hmac
import json
import secrets
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from app import auth
from app.intake import vault
from app.database import context
from app.models import Organization, User, now
from app.routes import DB, OperationKey
from app.security import rate_limit
from app.services import audit
from app.operations.common import command, record
from app.operations.models import Order
from app.events import feed
from . import client, fields, fleet, order_intake, outbound
from .models import ApiKey, ApiWebhook, TmsConnection, TmsInbox
from .schemas import ApiKeyCreated, ApiKeyView, ApiWebhookInput, ApiWebhookView, FleetSyncResult, InboxView, TmsCheckResult, TmsInput, TmsView, WebhookReceipt

router = APIRouter(prefix='/api/v1/companies/{slug}/integrations/tms', tags=['Connected TMS'])
Dispatcher = Annotated[User, Depends(auth.dispatcher)]
MESSAGES = {'HOST_NOT_FOUND': 'TMS address was not found.', 'HOST_NOT_ALLOWED': 'TMS address is not allowed.',
    'HTTPS_REQUIRED': 'The TMS address must use HTTPS.', 'CONNECTION_FAILED': 'Could not connect to the TMS.',
    'UNAUTHORIZED': 'The TMS rejected the API key or token.', 'FORBIDDEN': 'The API key or token cannot read drivers.',
    'NOT_FOUND': 'The drivers endpoint was not found.', 'RATE_LIMITED': 'The TMS is limiting requests. Try again shortly.',
    'REDIRECT_REFUSED': 'The TMS redirected the request. Use the final API address.', 'INVALID_JSON': 'The TMS did not return JSON.',
    'RESPONSE_TOO_LARGE': 'The TMS response is too large.', 'SERVER_ERROR': 'The TMS reported an error.', 'REQUEST_REJECTED': 'The TMS rejected the request.',
    'PAGINATION_HOST': 'The TMS pointed the next page to another address.', 'TOO_MANY_PAGES': 'The TMS returned more than 50 pages.'}


def connection(db, user, lock=False):
    query = select(TmsConnection).where(TmsConnection.organization_id == user.organization_id)
    return db.scalar(query.with_for_update() if lock else query)


def view(row):
    if row is None: return None
    result = TmsView.model_validate(row)
    result.webhook_secret_configured = row.webhook_secret is not None
    result.missing_fields = {record: fields.missing(record, row.field_maps.get(record, {})) for record in ('driver', 'vehicle', 'order')}
    return result.model_dump(mode='json')


def message(error: client.TmsError): return MESSAGES.get(error.code, 'The TMS could not be reached.')


@router.get('', response_model=TmsView | None)
def get_tms(slug: str, db: DB, user: Dispatcher):
    return view(connection(db, user))


@router.put('', response_model=TmsView)
def save_tms(slug: str, data: TmsInput, db: DB, key: OperationKey, user: Dispatcher):
    def run():
        row = connection(db, user, lock=True)
        if row and data.version != row.version: raise HTTPException(409, 'TMS settings changed. Reload before saving.')
        if row is None and data.credential is None: raise HTTPException(422, 'Enter the TMS API key or token.')
        if data.sync_method == 'webhook' and data.webhook_secret is None and (row is None or row.webhook_secret is None):
            raise HTTPException(422, 'Generate a webhook secret.')
        try: client.check_host(data.base_url)
        except client.TmsError as error: raise HTTPException(422, message(error)) from None
        first = row is None
        if first:
            row = TmsConnection(organization_id=user.organization_id, credential='', version=0, field_maps={})
            db.add(row)
        for name in ('enabled', 'base_url', 'auth_type', 'drivers_endpoint', 'vehicles_endpoint', 'fleet_sync_minutes', 'sync_method',
                'orders_endpoint', 'poll_interval_minutes', 'status_endpoint', 'field_maps'):
            setattr(row, name, getattr(data, name))
        if data.credential is not None: row.credential = vault.seal(data.credential)
        if data.webhook_secret is not None: row.webhook_secret = vault.seal(data.webhook_secret)
        row.last_error, row.version = None, row.version + 1
        db.flush()
        # Status delivery starts with events committed after the first save.
        if first: feed.consume(db, user.organization_id, outbound.TMS_STATUS.consumer, 1)
        audit(db, user, 'tms.updated', row.id, user.organization_id)
        return view(row)
    payload = data.model_dump(mode='json', exclude={'credential', 'webhook_secret'}) | {
        'credential_changed': data.credential is not None, 'webhook_secret_changed': data.webhook_secret is not None}
    return command(db, user, key, 'tms-save', payload, run)


@router.post('/test', response_model=TmsCheckResult)
def test_tms(slug: str, db: DB, user: Dispatcher):
    """Read the saved drivers endpoint with the saved credential. Nothing is imported."""
    rate_limit(f'tms-test:{user.id}', 10)
    row = connection(db, user)
    if row is None: raise HTTPException(404, 'Save the TMS connection first.')
    credential = vault.unseal(row.credential, 'TMS API key')
    try: client.request('GET', client.url(row.base_url, row.drivers_endpoint), client.headers(row.auth_type, credential))
    except client.TmsError as error: return {'ok': False, 'message': message(error)}
    return {'ok': True, 'message': 'Connected.'}


@router.post('/sync', response_model=FleetSyncResult)
def sync_fleet(slug: str, db: DB, user: Dispatcher):
    """Import drivers and vehicles now with the saved connection; also runs on the sync interval."""
    rate_limit(f'tms-sync:{user.organization_id}', 6)
    row = connection(db, user)
    if row is None: raise HTTPException(404, 'Save the TMS connection first.')
    if not row.enabled: raise HTTPException(409, 'Enable the TMS connection first.')
    result = fleet.sync(user.organization_id)
    if result and result['error']: raise HTTPException(502, MESSAGES.get(result['error'], 'The TMS could not be reached.'))
    return result


@router.get('/inbox', response_model=list[InboxView])
def list_inbox(slug: str, db: DB, user: Dispatcher, status: Literal['open', 'all'] = 'open', limit: int = Query(default=50, ge=1, le=200)):
    """Received TMS orders, newest first; `open` lists only those needing review or that failed."""
    query = select(TmsInbox).where(TmsInbox.organization_id == user.organization_id)
    if status == 'open': query = query.where(TmsInbox.status.in_(['NEEDS_REVIEW', 'FAILED']))
    rows = db.scalars(query.order_by(TmsInbox.received_at.desc(), TmsInbox.created_at.desc()).limit(limit)).all()
    numbers = dict(db.execute(select(Order.id, Order.number).where(Order.organization_id == user.organization_id,
        Order.id.in_([row.order_id for row in rows if row.order_id]))).all())
    return [{**InboxView.model_validate(row).model_dump(), 'order_number': numbers.get(row.order_id)} for row in rows]


def _reopen(db, user, identity, key, status, statuses):
    def run():
        row = record(db, TmsInbox, user, identity, lock=True)
        if row.status not in statuses: raise HTTPException(409, 'This TMS order is not waiting for review.')
        row.status, row.version = status, row.version + 1
        if status == 'RECEIVED': row.attempts, row.missing, row.error_code = 0, [], None
        audit(db, user, 'tms.order_retried' if status == 'RECEIVED' else 'tms.order_dismissed', row.id, user.organization_id)
        return {**InboxView.model_validate(row).model_dump(mode='json'), 'order_number': None}
    return command(db, user, key, f'tms-inbox-{status}:{identity}', {'id': str(identity)}, run)


@router.post('/inbox/{identity}/retry', response_model=InboxView)
def retry_inbox(slug: str, identity: UUID, db: DB, key: OperationKey, user: Dispatcher):
    """Process again after fixing the field map, catalogue or TMS data."""
    return _reopen(db, user, identity, key, 'RECEIVED', {'NEEDS_REVIEW', 'FAILED'})


@router.post('/inbox/{identity}/dismiss', response_model=InboxView)
def dismiss_inbox(slug: str, identity: UUID, db: DB, key: OperationKey, user: Dispatcher):
    return _reopen(db, user, identity, key, 'IGNORED', {'NEEDS_REVIEW', 'FAILED'})


receiver = APIRouter(tags=['Connected TMS'])


def signed(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    supplied = signature.strip().removeprefix('sha256=').lower()
    return hmac.compare_digest(expected, supplied)


@receiver.post('/api/v1/companies/{slug}/integrations/tms/events', status_code=202, response_model=WebhookReceipt)
async def receive_orders(slug: str, request: Request, db: DB, signature: Annotated[str, Header(alias='X-Signature', max_length=200)] = ''):
    """TMS webhook: HMAC-SHA256 of the raw body with the shared secret, hex, in `X-Signature`. Stored, then processed by the worker."""
    raw = await request.body()
    context(db, platform=True)
    company = db.scalar(select(Organization).where(Organization.slug == slug, Organization.active.is_(True)))
    row = db.scalar(select(TmsConnection).where(TmsConnection.organization_id == company.id)) if company else None
    if row is None or row.webhook_secret is None or not signature or not signed(vault.unseal(row.webhook_secret, 'webhook secret'), raw, signature):
        raise HTTPException(401, 'Invalid webhook signature.')
    if not row.enabled or row.sync_method != 'webhook': raise HTTPException(409, 'TMS webhooks are turned off for this company.')
    rate_limit(f'tms-webhook:{company.id}', 3000)
    try: payload = json.loads(raw)
    except ValueError: raise HTTPException(422, 'Send the order as JSON.') from None
    context(db, company.id)
    stored, rejected = order_intake.store(db, company.id, payload, row.field_maps.get('order', {}), 'WEBHOOK')
    if not stored and rejected: raise HTTPException(422, 'No TMS order ID was found at the mapped field.')
    return {'accepted': stored, 'rejected': rejected}


webhooks = APIRouter(prefix='/api/v1/companies/{slug}/integrations/webhooks', tags=['Dispatra API'])


def webhook_view(row):
    """JSON-ready: saved command results are stored as JSON for idempotent replay."""
    if row is None: return None
    return jsonable_encoder({'url': row.url, 'enabled': row.enabled, 'secret_configured': True, 'last_delivery_at': row.last_delivery_at,
        'last_error': row.last_error, 'version': row.version})


@webhooks.get('', response_model=ApiWebhookView | None)
def get_webhook(slug: str, db: DB, user: Dispatcher):
    return webhook_view(db.scalar(select(ApiWebhook).where(ApiWebhook.organization_id == user.organization_id)))


@webhooks.put('', response_model=ApiWebhookView)
def save_webhook(slug: str, data: ApiWebhookInput, db: DB, key: OperationKey, user: Dispatcher):
    """Signed order events for the company's own systems. The secret is required the first time and never returned."""
    def run():
        row = db.scalar(select(ApiWebhook).where(ApiWebhook.organization_id == user.organization_id).with_for_update())
        if row and data.version != row.version: raise HTTPException(409, 'Webhook settings changed. Reload before saving.')
        if row is None and data.secret is None: raise HTTPException(422, 'Generate a signing secret.')
        try: client.check_host(data.url)
        except client.TmsError as error: raise HTTPException(422, 'Webhook address is not allowed.' if error.code == 'HOST_NOT_ALLOWED' else 'Webhook address was not found.') from None
        first = row is None
        if first:
            row = ApiWebhook(organization_id=user.organization_id, version=0)
            db.add(row)
        row.url, row.enabled = data.url, data.enabled
        if data.secret is not None: row.secret = vault.seal(data.secret)
        row.last_error, row.version = None, row.version + 1
        db.flush()
        if first: feed.consume(db, user.organization_id, outbound.API_WEBHOOKS.consumer, 1)
        audit(db, user, 'webhook.updated', row.id, user.organization_id)
        return webhook_view(row)
    payload = data.model_dump(mode='json', exclude={'secret'}) | {'secret_changed': data.secret is not None}
    return command(db, user, key, 'api-webhook-save', payload, run)


api_keys = APIRouter(prefix='/api/v1/companies/{slug}/integrations/api-key', tags=['Dispatra API'])


def key_view(row):
    """JSON-ready: saved command results are stored as JSON for idempotent replay."""
    return jsonable_encoder({'configured': row is not None, 'prefix': row.prefix if row else None, 'created_at': row.created_at if row else None,
        'last_used_at': row.last_used_at if row else None})


@api_keys.get('', response_model=ApiKeyView)
def get_api_key(slug: str, db: DB, user: Dispatcher):
    return key_view(db.scalar(select(ApiKey).where(ApiKey.organization_id == user.organization_id, ApiKey.revoked_at.is_(None))))


@api_keys.post('', response_model=ApiKeyCreated, status_code=201)
def create_api_key(slug: str, db: DB, key: OperationKey, user: Dispatcher):
    """Create the company's key, revoking any previous one. The key is returned once and stored only as a hash."""
    secret = None
    def run():
        nonlocal secret
        current = db.scalar(select(ApiKey).where(ApiKey.organization_id == user.organization_id, ApiKey.revoked_at.is_(None)).with_for_update())
        if current:
            current.revoked_at, current.version = now(), current.version + 1
            db.flush()
        secret = 'dsp_' + secrets.token_hex(32)
        row = ApiKey(organization_id=user.organization_id, key_hash=hashlib.sha256(secret.encode()).hexdigest(), prefix=secret[:12])
        db.add(row); db.flush()
        audit(db, user, 'api_key.created', row.id, user.organization_id)
        return key_view(row)
    result = command(db, user, key, 'api-key-create', {}, run)
    if secret is None: raise HTTPException(409, 'This request was already used. Create a new key with a new request.')
    return {**result, 'key': secret}
