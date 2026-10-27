"""Persistent private company conversations, independent of Orders and duty."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.models import User, now
from app.operations.models import Driver, Shipper
from app.operations.common import command
from app.services import audit
from .models import Conversation, Message
from .service import person, mark, message_view, sender_name, unread


def participant(db, user, identity, lock=False, kind='driver'):
    person(user)
    role = kind.upper()
    if user.role not in {'DISPATCHER', role}: raise HTTPException(404, 'Chat not found.')
    if user.role == role and getattr(user, f'{kind}_id') != identity: raise HTTPException(404, 'Chat not found.')
    model = Driver if kind == 'driver' else Shipper
    query = select(model).where(model.organization_id == user.organization_id, model.id == identity)
    member = db.scalar(query.with_for_update() if lock else query)
    if member is None: raise HTTPException(404, 'Chat not found.')
    return member


def closed_reason(db, user, identity, kind='driver'):
    member = participant(db, user, identity, kind=kind)
    active_account = db.scalar(select(User.id).where(User.organization_id == user.organization_id,
        getattr(User, f'{kind}_id') == identity, User.role == kind.upper(), User.active.is_(True)))
    active = member.active if kind == 'driver' else member.status != 'INACTIVE'
    return None if active and member.archived_at is None and active_account else f'This {kind} account is inactive. The chat is read-only.'


def conversations(db, user, identity, kind='driver'):
    """Shipper company history includes older order-scoped dispatch messages, without moving them."""
    query = select(Conversation).where(Conversation.organization_id == user.organization_id,
        Conversation.kind == ('DISPATCH_DRIVER' if kind == 'driver' else 'DISPATCH_SHIPPER'),
        getattr(Conversation, f'{kind}_id') == identity)
    if kind == 'driver': query = query.where(Conversation.order_id.is_(None))
    return db.scalars(query.order_by(Conversation.created_at, Conversation.id)).all()


def existing(db, user, identity, kind='driver'):
    return next((row for row in conversations(db, user, identity, kind) if row.order_id is None), None)


def channel(db, user, identity, kind='driver'):
    member = participant(db, user, identity, kind=kind)
    rows = conversations(db, user, identity, kind)
    row = next((row for row in rows if row.order_id is None), rows[0] if rows else None)
    reason = closed_reason(db, user, identity, kind)
    counts = unread(db, user, [row.id for row in rows])
    name = member.name if kind == 'driver' else member.company_name or member.name
    dates = [row.last_message_at for row in rows if row.last_message_at]
    return dict(kind='DISPATCH_DRIVER' if kind == 'driver' else 'DISPATCH_SHIPPER', conversation_id=row.id if row else None,
        title=name if user.role == 'DISPATCHER' else 'Company', driver_id=identity if kind == 'driver' else None,
        shipper_id=identity if kind == 'shipper' else None, can_send=reason is None, closed_reason=reason,
        unread=sum(counts.values()), last_message_at=max(dates) if dates else None)


def shipper_inbox_item(db, user, identity):
    view = channel(db, user, identity, 'shipper')
    ids = [row.id for row in conversations(db, user, identity, 'shipper')]
    last = db.scalar(select(Message).where(Message.organization_id == user.organization_id,
        Message.conversation_id.in_(ids)).order_by(Message.seq.desc()).limit(1))
    return dict(**view, order_id=None, order_number=None, order_status=None,
        last_message=message_view(last, user) if last else None)


def send(db, user, identity, data, key, kind='driver'):
    participant(db, user, identity, kind=kind)
    def run():
        member = participant(db, user, identity, lock=True, kind=kind)
        reason = closed_reason(db, user, identity, kind)
        if reason: raise HTTPException(409, reason)
        shipper_id, driver_id = (identity, None) if kind == 'shipper' else (None, identity)
        db.execute(insert(Conversation).values(id=uuid4(), organization_id=user.organization_id,
            order_id=None, kind='DISPATCH_DRIVER' if kind == 'driver' else 'DISPATCH_SHIPPER',
            shipper_id=shipper_id, driver_id=driver_id, created_at=now()).on_conflict_do_nothing(constraint='conversations_order_kind_driver'))
        conversation = existing(db, user, identity, kind)
        name = (member.company_name or member.name) if user.role == 'SHIPPER' else sender_name(db, user, None)
        row = Message(id=uuid4(), organization_id=user.organization_id, conversation_id=conversation.id,
            shipper_id=shipper_id, driver_id=driver_id, sender_user_id=user.id, sender_role=user.role,
            sender_name=name, body=data.body, created_at=now())
        db.add(row); db.flush(); db.refresh(row)
        conversation.last_message_at = row.created_at
        mark(db, user, conversation, row.seq)
        audit(db, user, 'message.created', row.id, user.organization_id)
        return message_view(row, user)
    action = f'company-chat-message:{identity}' if kind == 'driver' else f'shipper-company-chat-message:{identity}'
    return command(db, user, key, action, data.model_dump(), run)
