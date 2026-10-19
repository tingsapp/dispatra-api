"""One persistent company/driver conversation, independent of assignments and duty."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.models import User, now
from app.operations.models import Driver
from app.operations.common import command
from app.services import audit
from .models import Conversation, Message
from .service import person, mark, message_view, sender_name, unread


def participant(db, user, driver_id, lock=False):
    person(user)
    if user.role not in {'DISPATCHER', 'DRIVER'}: raise HTTPException(404, 'Chat not found.')
    if user.role == 'DRIVER' and user.driver_id != driver_id: raise HTTPException(404, 'Chat not found.')
    query = select(Driver).where(Driver.organization_id == user.organization_id, Driver.id == driver_id)
    driver = db.scalar(query.with_for_update() if lock else query)
    if driver is None: raise HTTPException(404, 'Chat not found.')
    return driver


def closed_reason(db, user, driver_id):
    driver = participant(db, user, driver_id)
    active_account = db.scalar(select(User.id).where(User.organization_id == user.organization_id,
        User.driver_id == driver_id, User.role == 'DRIVER', User.active.is_(True)))
    return None if driver.active and driver.archived_at is None and active_account else 'This driver account is inactive. The chat is read-only.'


def existing(db, user, driver_id):
    return db.scalar(select(Conversation).where(Conversation.organization_id == user.organization_id,
        Conversation.order_id.is_(None), Conversation.kind == 'DISPATCH_DRIVER', Conversation.driver_id == driver_id))


def channel(db, user, driver_id):
    driver = participant(db, user, driver_id)
    row = existing(db, user, driver_id)
    reason = closed_reason(db, user, driver_id)
    counts = unread(db, user, [row.id]) if row else {}
    return dict(kind='DISPATCH_DRIVER', conversation_id=row.id if row else None,
        title=driver.name if user.role == 'DISPATCHER' else 'Company', driver_id=driver_id,
        can_send=reason is None, closed_reason=reason, unread=counts.get(row.id, 0) if row else 0,
        last_message_at=row.last_message_at if row else None)


def send(db, user, driver_id, data, key):
    participant(db, user, driver_id)
    def run():
        participant(db, user, driver_id, lock=True)
        reason = closed_reason(db, user, driver_id)
        if reason: raise HTTPException(409, reason)
        db.execute(insert(Conversation).values(id=uuid4(), organization_id=user.organization_id,
            order_id=None, kind='DISPATCH_DRIVER', shipper_id=None, driver_id=driver_id,
            created_at=now()).on_conflict_do_nothing(constraint='conversations_order_kind_driver'))
        conversation = existing(db, user, driver_id)
        row = Message(id=uuid4(), organization_id=user.organization_id, conversation_id=conversation.id,
            shipper_id=None, driver_id=driver_id, sender_user_id=user.id, sender_role=user.role,
            sender_name=sender_name(db, user, None), body=data.body, created_at=now())
        db.add(row); db.flush(); db.refresh(row)
        conversation.last_message_at = row.created_at
        mark(db, user, conversation, row.seq)
        audit(db, user, 'message.created', row.id, user.organization_id)
        return message_view(row, user)
    return command(db, user, key, f'company-chat-message:{driver_id}', data.model_dump(), run)
