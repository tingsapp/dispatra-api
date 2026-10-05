from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.security import rate_limit
from app.services import audit
from app.operations import orders
from app.operations.common import command, operational_shipper, record
from app.operations.models import Catalog, Order, Shipper
from app.operations.schemas import OrderView
from . import mailbox, vault
from .models import EmailIntake, MailboxConnection
from .schemas import (IntakeDetail, IntakeOrder, IntakeShipper, IntakeVersion, IntakeView, MailboxCheck, MailboxCheckResult,
    MailboxInput, MailboxView, Status)

router = APIRouter(prefix='/api/v1/companies/{slug}/email-intake', tags=['Order agent'])
Dispatcher = Annotated[User, Depends(auth.dispatcher)]
MESSAGES = {'HOST_NOT_FOUND': 'Mailbox server was not found.', 'HOST_NOT_ALLOWED': 'Mailbox server address is not allowed.',
    'CONNECTION_FAILED': 'Could not connect to the mailbox server.', 'AUTHENTICATION_FAILED': 'Mailbox sign-in failed. Check the username and app password.',
    'FOLDER_NOT_FOUND': 'Mailbox folder was not found.', 'FOLDER_STATUS_UNAVAILABLE': 'Mailbox folder could not be read.'}
OPEN = {'NEEDS_REVIEW', 'UNKNOWN_SENDER', 'FAILED', 'NOT_AN_ORDER'}


def connection(db, user, lock=False):
    query = select(MailboxConnection).where(MailboxConnection.organization_id == user.organization_id)
    return db.scalar(query.with_for_update() if lock else query)


def view(db, row, detail=False):
    result = (IntakeDetail if detail else IntakeView).model_validate(row)
    if row.shipper_id:
        shipper = db.get(Shipper, row.shipper_id)
        result.shipper_name = (shipper.company_name or shipper.name) if shipper else None
    if row.order_id:
        order = db.get(Order, row.order_id)
        result.order_number = order.number if order else None
    return result.model_dump(mode='json')


def intake(db, user, identity, version=None):
    row = record(db, EmailIntake, user, identity, lock=version is not None)
    if version is not None and row.version != version: raise HTTPException(409, 'Email changed. Reload before continuing.')
    return row


@router.get('/mailbox', response_model=MailboxView | None)
def get_mailbox(slug: str, db: DB, user: Dispatcher):
    return connection(db, user)


@router.put('/mailbox', response_model=MailboxView)
def save_mailbox(slug: str, data: MailboxInput, db: DB, key: OperationKey, user: Dispatcher):
    def run():
        row = connection(db, user, lock=True)
        if row and data.version != row.version: raise HTTPException(409, 'Mailbox settings changed. Reload before saving.')
        if row is None and data.password is None: raise HTTPException(422, 'Enter the mailbox app password.')
        if data.default_service_id:
            service = record(db, Catalog, user, data.default_service_id)
            if service.kind != 'SERVICE' or not service.active: raise HTTPException(422, 'Choose an active service level.')
        password = data.password if data.password is not None else vault.unseal(row.secret)
        moved = row is None or (row.host, row.port, row.username, row.folder) != (data.host, data.port, data.username, data.folder)
        if data.enabled and (moved or data.password is not None):
            try: mailbox.check(data.host, data.port, data.username, password, data.folder)
            except mailbox.MailboxError as error: raise HTTPException(422, MESSAGES.get(error.code, 'Mailbox could not be reached.')) from None
        if row is None:
            row = MailboxConnection(organization_id=user.organization_id, secret='', version=0)
            db.add(row)
        row.host, row.port, row.username, row.folder = data.host, data.port, data.username, data.folder
        row.enabled, row.default_service_id, row.secret = data.enabled, data.default_service_id, vault.seal(password)
        # A different mailbox starts at its current end; earlier mail is never imported.
        if moved: row.uid_validity, row.last_uid = None, 0
        row.last_error, row.version = None, row.version + 1
        db.flush()
        audit(db, user, 'mailbox.updated', row.id, user.organization_id)
        return MailboxView.model_validate(row).model_dump(mode='json')
    return command(db, user, key, 'mailbox-save', data.model_dump(mode='json', exclude={'password'}) | {'password_changed': data.password is not None}, run)


@router.post('/mailbox/test', response_model=MailboxCheckResult)
def test_mailbox(slug: str, data: MailboxCheck, db: DB, user: Dispatcher):
    rate_limit(f'mailbox-test:{user.id}', 10)
    row = connection(db, user)
    if data.password is None and row is None: raise HTTPException(422, 'Enter the mailbox app password.')
    password = data.password if data.password is not None else vault.unseal(row.secret)
    try: mailbox.check(data.host, data.port, data.username, password, data.folder)
    except mailbox.MailboxError as error: return {'ok': False, 'error': MESSAGES.get(error.code, 'Mailbox could not be reached.')}
    return {'ok': True}


@router.get('/messages', response_model=list[IntakeView])
def messages(slug: str, db: DB, user: Dispatcher, status: Annotated[list[Status] | None, Query()] = None, limit: int = Query(50, ge=1, le=200)):
    query = select(EmailIntake).where(EmailIntake.organization_id == user.organization_id)
    if status: query = query.where(EmailIntake.status.in_(status))
    return [view(db, row) for row in db.scalars(query.order_by(EmailIntake.received_at.desc(), EmailIntake.id.desc()).limit(limit))]


@router.get('/messages/{identity}', response_model=IntakeDetail)
def message(slug: str, identity: UUID, db: DB, user: Dispatcher):
    return view(db, intake(db, user, identity), detail=True)


def change(db, user, key, name, identity, data, allowed, apply):
    def run():
        row = intake(db, user, identity, data.version)
        if row.status not in allowed: raise HTTPException(409, 'This email can no longer be changed.')
        apply(row)
        row.version += 1
        db.flush()
        audit(db, user, 'intake.' + name, row.id, user.organization_id)
        return view(db, row)
    return command(db, user, key, 'intake-' + name, {'id': str(identity), **data.model_dump(mode='json')}, run)


@router.post('/messages/{identity}/discard', response_model=IntakeView)
def discard(slug: str, identity: UUID, data: IntakeVersion, db: DB, key: OperationKey, user: Dispatcher):
    return change(db, user, key, 'discarded', identity, data, OPEN, lambda row: setattr(row, 'status', 'DISCARDED'))


def reopen(row):
    row.status, row.attempts, row.error_code = 'RECEIVED', 0, None


@router.post('/messages/{identity}/retry', response_model=IntakeView, status_code=202)
def retry(slug: str, identity: UUID, data: IntakeVersion, db: DB, key: OperationKey, user: Dispatcher):
    """Ask the Order agent to read the email again, for example after fixing the Shipper profile or AI settings."""
    return change(db, user, key, 'retried', identity, data, OPEN | {'DISCARDED'}, reopen)


@router.post('/messages/{identity}/shipper', response_model=IntakeView, status_code=202)
def link_shipper(slug: str, identity: UUID, data: IntakeShipper, db: DB, key: OperationKey, user: Dispatcher):
    """The dispatcher vouches that the email is from this Shipper; the agent then reads it again on their behalf."""
    shipper = operational_shipper(db, user, data.shipper_id)
    if shipper.status != 'ACTIVE': raise HTTPException(409, 'Shipper account must be active to book.')
    def apply(row):
        row.shipper_id, row.sender_verified = shipper.id, True
        reopen(row)
    return change(db, user, key, 'shipper_linked', identity, data, OPEN, apply)


@router.post('/messages/{identity}/order', response_model=OrderView, status_code=201)
def create_order(slug: str, identity: UUID, data: IntakeOrder, db: DB, key: OperationKey, user: Dispatcher):
    """Dispatcher completes the draft; the Order is created through the normal booking service with source EMAIL."""
    def run():
        row = intake(db, user, identity, data.version)
        if row.status not in OPEN: raise HTTPException(409, 'This email can no longer be turned into an Order.')
        order = orders.create_order(db, user, data.booking, f'intake-{identity}', source='EMAIL')
        row.status, row.order_id, row.shipper_id, row.missing = 'ORDER_CREATED', UUID(str(order['id'])), UUID(str(order['shipper_id'])), []
        row.version += 1
        db.flush()
        audit(db, user, 'intake.order_created', row.id, user.organization_id)
        return order
    return command(db, user, key, 'intake-order', {'id': str(identity), **data.model_dump(mode='json')}, run)
