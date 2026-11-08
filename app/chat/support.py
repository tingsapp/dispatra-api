"""Private platform-admin to dispatcher channels. No operational chats enter admin reads."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.models import Organization, User, now
from app.services import audit, once
from .models import Conversation, Message
from . import service


def participant(db, user, dispatcher_id, lock=False):
    service.person(user)
    if user.role not in {'ADMIN', 'DISPATCHER'} or (user.role == 'DISPATCHER' and user.id != dispatcher_id):
        raise HTTPException(404, 'Chat not found.')
    query = select(User).where(User.id == dispatcher_id, User.role == 'DISPATCHER')
    if user.role == 'DISPATCHER': query = query.where(User.organization_id == user.organization_id)
    dispatcher = db.scalar(query.with_for_update() if lock else query)
    if dispatcher is None: raise HTTPException(404, 'Chat not found.')
    return dispatcher


def channel(db, user, dispatcher_id):
    dispatcher = participant(db, user, dispatcher_id)
    org = db.get(Organization, dispatcher.organization_id)
    conversation = db.scalar(select(Conversation).where(Conversation.organization_id == org.id,
        Conversation.dispatcher_id == dispatcher.id, Conversation.kind == 'ADMIN_DISPATCHER'))
    reason = None if dispatcher.active and org.active else 'This dispatcher account or company is inactive. The chat is read-only.'
    return dict(kind='ADMIN_DISPATCHER', conversation_id=conversation.id if conversation else None,
        dispatcher_id=dispatcher.id, company_name=org.name, title=(dispatcher.display_name or dispatcher.login_id) if user.role == 'ADMIN' else 'Admin',
        driver_id=None, can_send=reason is None, closed_reason=reason,
        unread=service.unread(db, user, [conversation.id]).get(conversation.id, 0) if conversation else 0,
        last_message_at=conversation.last_message_at if conversation else None)


def send(db, user, dispatcher_id, data, key):
    participant(db, user, dispatcher_id)
    def run():
        dispatcher = participant(db, user, dispatcher_id, lock=True)
        view = channel(db, user, dispatcher_id)
        if not view['can_send']: raise HTTPException(409, view['closed_reason'])
        db.execute(insert(Conversation).values(id=uuid4(), organization_id=dispatcher.organization_id,
            kind='ADMIN_DISPATCHER', dispatcher_id=dispatcher_id, created_at=now()).on_conflict_do_nothing(constraint='conversations_order_kind_driver'))
        conversation = db.scalar(select(Conversation).where(Conversation.organization_id == dispatcher.organization_id,
            Conversation.dispatcher_id == dispatcher_id, Conversation.kind == 'ADMIN_DISPATCHER'))
        row = Message(id=uuid4(), organization_id=dispatcher.organization_id, conversation_id=conversation.id,
            sender_user_id=user.id, sender_role=user.role, sender_name=(user.display_name or ('Admin' if user.role == 'ADMIN' else 'Dispatch'))[:160], body=data.body, created_at=now())
        db.add(row); db.flush(); db.refresh(row)
        conversation.last_message_at = row.created_at
        service.mark(db, user, conversation, row.seq)
        audit(db, user, 'message.created', row.id, dispatcher.organization_id)
        return service.message_view(row, user)
    return once(db, user, key, f'admin-chat:{dispatcher_id}', data.model_dump(include={'body'}), run)


def contacts(db, user, search='', cursor=None, limit=50):
    if user.role != 'ADMIN': raise HTTPException(403, 'Platform owner access required.')
    query = select(User, Organization).join(Organization, User.organization_id == Organization.id).where(User.role == 'DISPATCHER', User.active.is_(True))
    if cursor: query = query.where(User.id > cursor)
    if search.strip():
        pattern = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.where(User.display_name.ilike(pattern, escape='\\') | User.login_id.ilike(pattern, escape='\\') | Organization.name.ilike(pattern, escape='\\'))
    rows = db.execute(query.order_by(User.id).limit(limit + 1)).all()
    return {'items': [dict(id=u.id, name=u.display_name or u.login_id, company_name=o.name) for u,o in rows[:limit]],
        'next_cursor': rows[limit-1][0].id if len(rows) > limit else None}
