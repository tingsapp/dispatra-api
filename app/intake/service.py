"""Order agent use cases. Network calls (IMAP, OpenAI, geocoding) run between short transactions, never inside one.

An Order is created only through `orders.create_order`, acting as the matched Shipper's own account with the
`AGENT` actor type, so Shipper permissions, pricing, idempotency, audit and events all apply unchanged.
"""
import json
from types import SimpleNamespace
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.database import context, engine
from app.events import publish
from app.models import Organization, User, now
from app.operations import orders
from app.operations.common import settings
from app.operations.models import Catalog, Shipper
from app.operations.schemas import Booking
from . import booking, extract, geocode, mailbox, vault, batch
from .models import EmailIntake, MailboxConnection
from app.agents.telemetry import step

MAX_ATTEMPTS = 3
SYSTEM = SimpleNamespace(id=None, role=None)


def reopen(row):
    """Shared retry transition used by dispatcher commands and approved technical repairs."""
    row.status, row.attempts, row.error_code = 'RECEIVED', 0, None
    row.completion_requested_at = row.completion_submitted_at = None


class AgentActor:
    """The Shipper's own account, acting through the Order agent: same permissions, audited as `AGENT`."""
    actor_type = 'AGENT'

    def __init__(self, user): self._user = user

    def __getattr__(self, name): return getattr(self._user, name)


def signal(db, action, intake):
    publish.record(db, SYSTEM, action, intake.id, intake.organization_id, 'AGENT')


def companies(model, *conditions):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        return db.scalars(select(model.organization_id).distinct().where(*conditions)).all()


def mailboxes(): return companies(MailboxConnection, MailboxConnection.enabled.is_(True))


def pending(): return companies(EmailIntake, EmailIntake.status == 'RECEIVED', EmailIntake.attempts < MAX_ATTEMPTS)


def _poll_target(db, organization_id):
    return db.scalar(select(MailboxConnection).join(Organization, Organization.id == MailboxConnection.organization_id)
        .where(MailboxConnection.organization_id == organization_id, MailboxConnection.enabled.is_(True),
            Organization.active.is_(True)).with_for_update(of=MailboxConnection))


def _mailbox_failure(db, row, code):
    first = row.last_error is None
    row.last_error, row.last_polled_at = code, now()
    if first:
        # Every failure episode, including unreadable credentials, needs a fresh report identity.
        row.version += 1
        publish.record(db, SYSTEM, 'mailbox.failed', row.id, row.organization_id, 'AGENT')


def poll_mailbox(organization_id: UUID, *, expected_target: tuple[UUID, int] | None = None):
    """Fetch/store each message once. An approved retry may pin the mailbox identity/version."""
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = _poll_target(db, organization_id)
        if row is None or (expected_target is not None and (row.id, row.version) != expected_target): return 0
        try: password = vault.unseal(row.secret)
        except HTTPException:
            _mailbox_failure(db, row, 'PASSWORD_UNREADABLE')
            return 0
        observed = (row.id, row.version, row.last_polled_at)
        settings_ = (row.host, row.port, row.username, password, row.folder, row.uid_validity, row.last_uid)
    try: validity, last_uid, fetched = mailbox.fetch_new(*settings_)
    except mailbox.MailboxError as error:
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            row = _poll_target(db, organization_id)
            if row and (row.id, row.version, row.last_polled_at) == observed:
                _mailbox_failure(db, row, error.code)
        return 0
    stored = 0
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        row = _poll_target(db, organization_id)
        if row is None or (row.id, row.version, row.last_polled_at) != observed: return 0
        for message in fetched:
            parsed = mailbox.parse(message.raw)
            values = dict(organization_id=organization_id, message_id=parsed.message_id, mailbox_uid=message.uid,
                received_at=parsed.received_at, from_address=parsed.from_address, from_name=parsed.from_name, subject=parsed.subject,
                body='' if message.too_large else parsed.body, sender_verified=parsed.sender_verified,
                attachment_secret=vault.seal(json.dumps(parsed.attachments)) if parsed.attachments and not message.too_large else None,
                status='FAILED' if message.too_large or not parsed.from_address else 'RECEIVED', missing=[], summary='',
                error_code='MESSAGE_TOO_LARGE' if message.too_large else None if parsed.from_address else 'NO_SENDER', attempts=0)
            identity = db.scalar(insert(EmailIntake).values(id=uuid4(), version=1, created_at=now(), updated_at=now(), **values)
                .on_conflict_do_nothing(constraint='email_intakes_message').returning(EmailIntake.id))
            if identity:
                stored += 1
                if values['status'] == 'FAILED': signal(db, 'intake.failed', db.get(EmailIntake, identity))
                else: signal(db, 'intake.received', db.get(EmailIntake, identity))
        # Only the current snapshot may advance the cursor; messages also deduplicate by Message-ID.
        row.last_uid = max(row.last_uid, last_uid) if row.uid_validity == validity else last_uid
        row.uid_validity, row.last_polled_at, row.last_error = validity, now(), None
    return stored


def _choices(db, organization_id, kind):
    rows = db.scalars(select(Catalog).where(Catalog.organization_id == organization_id, Catalog.kind == kind, Catalog.active.is_(True)).order_by(Catalog.code))
    return [{'id': str(row.id), 'code': row.code, 'name': row.data.get('name', row.code),
        'description': row.data.get('description', ''),
        **({field: row.data.get(field) for field in ('payload_kg', 'volume_m3', 'length_cm', 'width_cm', 'height_cm', 'pallet_capacity', 'equipment')} if kind == 'VEHICLE_TYPE' else {})} for row in rows]


def _match(db, intake):
    if intake.shipper_id: return db.get(Shipper, intake.shipper_id)
    return db.scalar(select(Shipper).where(Shipper.organization_id == intake.organization_id, func.lower(func.trim(Shipper.email)) == intake.from_address.strip().lower()).order_by(Shipper.created_at).limit(1))


def _claim(organization_id, intake_id):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        intake = db.scalar(select(EmailIntake).where(EmailIntake.organization_id == organization_id, EmailIntake.id == intake_id,
            EmailIntake.status == 'RECEIVED').with_for_update(skip_locked=True))
        if intake is None or intake.attempts >= MAX_ATTEMPTS: return None
        intake.attempts += 1
        shipper = _match(db, intake)
        if shipper and not intake.shipper_id: intake.shipper_id = shipper.id
        actor = SimpleNamespace(organization_id=organization_id)
        _, config = settings(db, actor)
        services = _choices(db, organization_id, 'SERVICE')
        default = str(config.default_service_id) if config.default_service_id else None
        try: files=json.loads(vault.unseal(intake.attachment_secret,'email attachments')) if intake.attachment_secret else []
        except (HTTPException, ValueError): files=[{'error':'Saved email attachments could not be read and require review.'}]
        return {'email': {'subject': intake.subject, 'body': intake.body, 'received_at': intake.received_at.isoformat(), 'attachments': files},
            'saved_booking': intake.extraction if intake.extraction and intake.extraction.get('_email_batch') else None,
            'verified': intake.sender_verified,
            'context': {'time_zone': config.time_zone, 'services': services, 'vehicle_types': _choices(db, organization_id, 'VEHICLE_TYPE'),
                'accessorials': _choices(db, organization_id, 'ACCESSORIAL'), 'dimension_unit': config.dimension_unit, 'weight_unit': config.weight_unit,
                'default_service_id': default if any(s['id'] == default for s in services) else None,
                'shipper': {'id': str(shipper.id), 'name': shipper.name, 'company_name': shipper.company_name,
                    'contact_name': shipper.contact_name, 'phone': shipper.phone,
                    'warehouse': shipper.warehouse, 'status': shipper.status} if shipper else None}}


def _finish(organization_id, intake_id, apply):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        intake = db.scalar(select(EmailIntake).where(EmailIntake.organization_id == organization_id, EmailIntake.id == intake_id).with_for_update())
        if intake is None or intake.status != 'RECEIVED': return
        apply(db, intake)
        intake.version += 1


def _review(db, intake, draft, missing, action='intake.needs_review'):
    intake.status, intake.draft, intake.missing, intake.error_code = 'NEEDS_REVIEW', draft, missing, None
    signal(db, action, intake)


def _create(db, intake, draft):
    user = db.scalar(select(User).where(User.organization_id == intake.organization_id, User.shipper_id == intake.shipper_id,
        User.role == 'SHIPPER', User.active.is_(True)))
    if user is None: return _review(db, intake, draft, ['Shipper portal account is inactive'])
    context(db, intake.organization_id, False, intake.shipper_id)
    try:
        with db.begin_nested():
            order = orders.create_order(db, AgentActor(user), Booking.model_validate(draft), f'email-intake-{intake.id}', source='EMAIL')
    except HTTPException as error:
        context(db, intake.organization_id)
        return _review(db, intake, draft, [f'Order could not be created: {error.detail}'])
    context(db, intake.organization_id)
    intake.status, intake.draft, intake.missing, intake.order_id, intake.error_code = 'ORDER_CREATED', draft, [], UUID(str(order['id'])), None
    signal(db, 'intake.order_created', intake)


def process(organization_id: UUID, intake_id: UUID):
    step('Claiming the next queued email.', organization_id)
    claimed = _claim(organization_id, intake_id)
    if claimed is None: return
    issues=[file['error'] for file in claimed['email'].get('attachments',[]) if file.get('error')]
    if issues: return _finish(organization_id, intake_id, lambda db,intake: _review(db,intake,None,list(dict.fromkeys(issues))))
    ctx = claimed['context']
    prompt_shipper = {'name': ctx['shipper'].get('company_name') or ctx['shipper']['name'], 'warehouse_city': (ctx['shipper']['warehouse'] or {}).get('city')} if ctx['shipper'] else None
    step('Extracting booking facts with the Order agent.', organization_id)
    try: result = extract.Extraction.model_validate(claimed['saved_booking']) if claimed.get('saved_booking') else extract.extract(claimed['email'], {**ctx, 'shipper': prompt_shipper})
    except extract.Unavailable as error:
        step('Order agent extraction unavailable; retry policy applied.', organization_id, 'WARNING')
        def failed(db, intake):
            if error.code == 'ATTACHMENT_UNREADABLE':
                return _review(db,intake,None,['The attachments could not be read reliably. Review the email or request a clearer file.'])
            intake.error_code = error.code
            if not error.retry or intake.attempts >= MAX_ATTEMPTS:
                intake.status = 'FAILED'
                signal(db, 'intake.failed', intake)
        return _finish(organization_id, intake_id, failed)
    step('Validating booking facts and addresses.',organization_id)
    if isinstance(result,extract.ExtractionBatch):
        if result.review_reasons:
            def unclear(db,row):
                row.extraction=result.model_dump(mode='json');row.summary=result.summary[:500]
                _review(db,row,None,result.review_reasons)
            return _finish(organization_id,intake_id,unclear)
        prepared=[_prepare(booking_result,ctx) for booking_result in result.orders]
        def split(db,parent):
            batch.save(db,parent,prepared,lambda db,row,item,draft,missing:_settle(db,row,item,draft,missing,ctx,claimed['verified']))
        _finish(organization_id,intake_id,split)
    else:
        item,draft,missing=_prepare(result,ctx)
        _finish(organization_id,intake_id,lambda db,row:_settle(db,row,item,draft,missing,ctx,claimed['verified']))
    step('Booking validation finished; result saved.',organization_id)


def _prepare(result,ctx):
    if not result.is_order_request: return result,None,[]
    draft,missing=booking.build(result,ctx,geocode.verify)
    return result,draft,missing


def _settle(db,intake,result,draft,missing,ctx,verified):
    metadata=(intake.extraction or {}).get('_email_batch')
    intake.extraction=result.model_dump(mode='json')
    if metadata: intake.extraction['_email_batch']=metadata
    intake.summary=result.summary[:500];intake.error_code=None
    if metadata: intake.summary=f"Order {metadata['index']} of {metadata['total']}: {intake.summary}"[:500]
    if result.review_reasons: return _review(db,intake,draft,result.review_reasons)
    if not result.is_order_request:
        intake.status='NOT_AN_ORDER';return
    if ctx['shipper'] is None:
        intake.status,intake.draft,intake.missing='UNKNOWN_SENDER',draft,['No Shipper in this company matches the sender email',*missing]
        signal(db,'intake.unknown_sender',intake);return
    if ctx['shipper'].get('status', 'ACTIVE') != 'ACTIVE':
        missing=[f"Shipper account is {ctx['shipper']['status'].lower().replace('_', ' ')}",*missing]
    # A known company Shipper is authoritative for email intake. Mail authentication is
    # retained as diagnostic metadata, but does not block its matched booking.
    if missing: _review(db,intake,draft,missing)
    else: _create(db,intake,draft)


def process_pending(organization_id: UUID, limit=25):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        identities = db.scalars(select(EmailIntake.id).where(EmailIntake.organization_id == organization_id, EmailIntake.status == 'RECEIVED',
            EmailIntake.attempts < MAX_ATTEMPTS).order_by(EmailIntake.received_at).limit(limit)).all()
    for identity in identities: process(organization_id, identity)
    return len(identities)
