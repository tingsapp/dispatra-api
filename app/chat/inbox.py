"""Authorized, searchable keyset pages of logical conversations, including preserved history."""
import base64
import json
from datetime import datetime
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import case, or_, select, tuple_
from app.models import Organization, User
from app.operations.models import Driver, Order, Shipper
from .models import Conversation, Message
from . import service


def item(db, user, conversation, order):
    from . import company, support
    if conversation.kind == 'ADMIN_DISPATCHER':
        view = support.channel(db, user, conversation.dispatcher_id)
    elif conversation.kind == 'DISPATCH_SHIPPER':
        return company.shipper_inbox_item(db, user, conversation.shipper_id)
    elif order is None:
        view = company.channel(db, user, conversation.driver_id)
    else:
        driver = service.driver_names(db, user, [conversation.driver_id]).get(conversation.driver_id)
        reason = service.closed_reason(user, conversation.kind, order, conversation.driver_id, service.current_driver(db, user, order))
        view = dict(kind=conversation.kind, conversation_id=conversation.id, title=service.title(user, conversation.kind, order, driver),
            can_send=reason is None, closed_reason=reason, driver_id=conversation.driver_id,
            unread=service.unread(db, user, [conversation.id]).get(conversation.id, 0), last_message_at=conversation.last_message_at)
    last = db.scalar(select(Message).where(Message.organization_id == conversation.organization_id,
        Message.conversation_id == conversation.id).order_by(Message.seq.desc()).limit(1))
    return dict(**view, order_id=order.id if order else None, order_number=order.number if order else None,
        order_status=order.status if order else None, last_message=service.message_view(last, user) if last else None)


def detail(db, user, identity):
    return item(db, user, *service.conversation_for(db, user, identity))


def page(db, user, limit=50, cursor=None, search='', kind=None, unread_only=False):
    service.person(user)
    group = case((Conversation.kind == 'DISPATCH_SHIPPER', Conversation.shipper_id), else_=Conversation.id)
    with service.scope(db, user):
        latest = (service.visible(user, Conversation.id).where(Conversation.last_message_at.is_not(None))
            .distinct(group).order_by(group, Conversation.last_message_at.desc(), Conversation.id.desc()).subquery())
        query = service.visible(user, Conversation, Order).where(Conversation.id.in_(select(latest.c.id)))
        if kind: query = query.where(Conversation.kind == kind)
        if search.strip():
            pattern = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            query = query.outerjoin(Driver, Driver.id == Conversation.driver_id).outerjoin(Shipper, Shipper.id == Conversation.shipper_id).outerjoin(User, User.id == Conversation.dispatcher_id).join(Organization, Organization.id == Conversation.organization_id)
            # Match any preserved shipper message, not only the latest underlying order conversation.
            matching = select(Message.id).where(Message.organization_id == Conversation.organization_id,
                or_(Message.conversation_id == Conversation.id, (Conversation.kind == 'DISPATCH_SHIPPER') & (Message.shipper_id == Conversation.shipper_id) & (Message.conversation_id.in_(select(Conversation.id).where(Conversation.kind == 'DISPATCH_SHIPPER')))),
                Message.body.ilike(pattern, escape='\\')).correlate(Conversation).exists()
            query = query.where(or_(Driver.name.ilike(pattern, escape='\\'), Shipper.name.ilike(pattern, escape='\\'),
                Shipper.company_name.ilike(pattern, escape='\\'), User.display_name.ilike(pattern, escape='\\'), User.login_id.ilike(pattern, escape='\\'),
                Organization.name.ilike(pattern, escape='\\') if user.role == 'ADMIN' else Conversation.kind.ilike(pattern, escape='\\'),
                Order.number.ilike(pattern, escape='\\'), matching))
        if unread_only:
            counts = service.unread(db, user)
            unread_ids = list(counts)
            unread_shippers = select(Conversation.shipper_id).where(Conversation.id.in_(unread_ids), Conversation.kind == 'DISPATCH_SHIPPER')
            query = query.where(or_(Conversation.id.in_(unread_ids), (Conversation.kind == 'DISPATCH_SHIPPER') & Conversation.shipper_id.in_(unread_shippers)))
        if cursor:
            try:
                stamp, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                anchor = datetime.fromisoformat(stamp), UUID(identity)
                if anchor[0].tzinfo is None: raise ValueError()
            except (ValueError, TypeError, KeyError): raise HTTPException(422, 'Invalid chat cursor.')
            query = query.where(tuple_(Conversation.last_message_at, Conversation.id) < anchor)
        rows = db.execute(query.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit + 1)).all()
        selected = rows[:limit]
        result = [item(db, user, conversation, order) for conversation, order in selected]
    last = selected[-1][0] if len(rows) > limit else None
    next_cursor = base64.urlsafe_b64encode(json.dumps([last.last_message_at.isoformat(), str(last.id)]).encode()).decode() if last else None
    return {'items': result, 'next_cursor': next_cursor}
