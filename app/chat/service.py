"""Order chat rules: who can read and send in each of an Order's three two-party conversations.

- Dispatcher ↔ Shipper: open from New until Completed/Cancelled; every dispatcher shares it.
- Dispatcher ↔ Driver and Driver ↔ Shipper: open while the Order is Assigned or In progress, with the
  Order's current driver only. A released/reassigned driver loses all access; a new driver starts fresh.
- Dispatchers read Driver ↔ Shipper chats (oversight) but never send there.
- After Completed or Cancelled every chat is read-only history.
RLS admits only a participant's rows; these checks add the current-driver and status rules.
"""
from contextlib import nullcontext
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from app.models import now
from app.services import audit
from app.operations.common import command
from app.operations.models import Driver, Order, Route
from app.operations.orders import company_scope
from .models import Conversation, Message, ReadPosition
from .schemas import MessageView

KINDS = ('DISPATCH_SHIPPER', 'DISPATCH_DRIVER', 'DRIVER_SHIPPER')
ROLE_KINDS = {'DISPATCHER': KINDS, 'SHIPPER': ('DISPATCH_SHIPPER', 'DRIVER_SHIPPER'), 'DRIVER': ('DISPATCH_DRIVER', 'DRIVER_SHIPPER')}
SENDERS = {'DISPATCH_SHIPPER': {'DISPATCHER', 'SHIPPER'}, 'DISPATCH_DRIVER': {'DISPATCHER', 'DRIVER'}, 'DRIVER_SHIPPER': {'DRIVER', 'SHIPPER'}}
OPEN = {'DISPATCH_SHIPPER': {'NEW', 'ASSIGNED', 'IN_PROGRESS'}, 'DISPATCH_DRIVER': {'ASSIGNED', 'IN_PROGRESS'}, 'DRIVER_SHIPPER': {'ASSIGNED', 'IN_PROGRESS'}}
DISPATCH = 'Dispatch'


def person(user):
    """Chat is between people; API keys and other system principals cannot read or send."""
    if user.role not in ROLE_KINDS or getattr(user, 'id', None) is None: raise HTTPException(403, 'Chat requires a signed-in dispatcher, Shipper or driver.')


def scope(db, user):
    # Shipper sessions cannot read routes/drivers; widen only for explicit, already-filtered queries.
    return company_scope(db, user) if user.role == 'SHIPPER' else nullcontext()


def current_driver(db, user, order):
    if not order.route_id: return None
    with company_scope(db, user):
        return db.scalar(select(Route.driver_id).where(Route.organization_id == user.organization_id, Route.id == order.route_id))


def order_for(db, user, identity):
    """The Order and its current driver, or 404 when the caller is not a participant."""
    person(user)
    order = db.scalar(select(Order).where(Order.organization_id == user.organization_id, Order.id == identity))
    if order is None or (user.role == 'SHIPPER' and order.shipper_id != user.shipper_id): raise HTTPException(404, 'Order not found.')
    current = current_driver(db, user, order)
    if user.role == 'DRIVER' and current != user.driver_id: raise HTTPException(404, 'Order not found.')
    return order, current


def visible(user, *columns):
    current = select(Route.driver_id).where(Route.organization_id == Order.organization_id, Route.id == Order.route_id).scalar_subquery()
    query = select(*columns).select_from(Conversation).join(Order, and_(Order.organization_id == Conversation.organization_id,
        Order.id == Conversation.order_id)).where(Conversation.organization_id == user.organization_id)
    if user.role == 'SHIPPER':
        return query.where(Order.shipper_id == user.shipper_id, Conversation.shipper_id == user.shipper_id,
            or_(Conversation.kind == 'DISPATCH_SHIPPER', Conversation.driver_id == current))
    if user.role == 'DRIVER': return query.where(Conversation.driver_id == user.driver_id, current == user.driver_id)
    return query


def unread(db, user, ids=None):
    """Unread messages per conversation from the other party. Dispatchers do not count chats they only oversee."""
    convs = visible(user, Conversation.id)
    if user.role == 'DISPATCHER': convs = convs.where(Conversation.kind != 'DRIVER_SHIPPER')
    if ids is not None:
        if not ids: return {}
        convs = convs.where(Conversation.id.in_(ids))
    convs = convs.subquery()
    query = (select(Message.conversation_id, func.count()).join(convs, convs.c.id == Message.conversation_id)
        .outerjoin(ReadPosition, and_(ReadPosition.conversation_id == Message.conversation_id, ReadPosition.user_id == user.id))
        .where(Message.organization_id == user.organization_id, Message.sender_role != user.role,
            Message.seq > func.coalesce(ReadPosition.last_read_seq, 0)).group_by(Message.conversation_id))
    with scope(db, user): return dict(db.execute(query).all())


def total_unread(db, user):
    if user.role not in ROLE_KINDS or getattr(user, 'id', None) is None: return 0
    return sum(unread(db, user).values())


def driver_names(db, user, ids):
    ids = {identity for identity in ids if identity}
    if not ids: return {}
    with company_scope(db, user):
        return dict(db.execute(select(Driver.id, Driver.name).where(Driver.organization_id == user.organization_id, Driver.id.in_(ids))).all())


def shipper_name(order):
    shipper = order.booking.get('shipper') or {}
    return shipper.get('company_name') or shipper.get('name') or 'Shipper'


def title(user, kind, order, driver):
    driver, shipper = driver or 'Driver', shipper_name(order)
    if user.role == 'DISPATCHER': return {'DISPATCH_SHIPPER': shipper, 'DISPATCH_DRIVER': driver, 'DRIVER_SHIPPER': f'{driver} ↔ {shipper}'}[kind]
    if user.role == 'SHIPPER': return DISPATCH if kind == 'DISPATCH_SHIPPER' else driver
    return DISPATCH if kind == 'DISPATCH_DRIVER' else shipper


def closed_reason(user, kind, order, driver_id, current):
    """Why the caller cannot send in this chat, or None when they can."""
    if user.role not in SENDERS[kind]: return 'Dispatch can read this chat but not reply.'
    if order.status in {'COMPLETED', 'CANCELLED'}: return f'This order is {order.status.lower()}. The chat is read-only.'
    if order.status not in OPEN[kind]: return 'This chat opens when a driver is assigned.'
    if kind != 'DISPATCH_SHIPPER' and (current is None or driver_id != current): return 'This driver is no longer assigned.'
    return None


def message_view(row, user):
    return MessageView(id=row.id, seq=row.seq, conversation_id=row.conversation_id, sender_role=row.sender_role,
        sender_name=row.sender_name, mine=row.sender_user_id == user.id, body=row.body, created_at=row.created_at).model_dump(mode='json')


def channels(db, user, order_id):
    order, current = order_for(db, user, order_id)
    rows = db.scalars(select(Conversation).where(Conversation.organization_id == user.organization_id, Conversation.order_id == order.id)
        .order_by(Conversation.created_at)).all()
    wanted = [('DISPATCH_SHIPPER', None)] if user.role != 'DRIVER' else []
    if current:
        if user.role != 'SHIPPER': wanted.append(('DISPATCH_DRIVER', current))
        wanted.append(('DRIVER_SHIPPER', current))
    # Dispatchers keep previous drivers' chats as read-only history.
    if user.role == 'DISPATCHER':
        wanted += [(row.kind, row.driver_id) for row in sorted(rows, key=lambda row: KINDS.index(row.kind)) if (row.kind, row.driver_id) not in wanted]
    existing = {(row.kind, row.driver_id): row for row in rows}
    counts = unread(db, user, [row.id for row in rows])
    names = driver_names(db, user, [driver for _, driver in wanted])
    result = []
    for kind, driver_id in wanted:
        row = existing.get((kind, driver_id))
        reason = closed_reason(user, kind, order, driver_id, current)
        result.append(dict(kind=kind, conversation_id=row.id if row else None, title=title(user, kind, order, names.get(driver_id)),
            driver_id=driver_id if user.role == 'DISPATCHER' else None, can_send=reason is None, closed_reason=reason,
            unread=counts.get(row.id, 0) if row else 0, last_message_at=row.last_message_at if row else None))
    return result


def conversation_for(db, user, identity):
    person(user)
    with scope(db, user): row = db.execute(visible(user, Conversation, Order).where(Conversation.id == identity)).first()
    if row is None: raise HTTPException(404, 'Chat not found.')
    return row


def messages(db, user, identity, after=None, before=None, limit=50):
    conversation, _ = conversation_for(db, user, identity)
    query = select(Message).where(Message.organization_id == user.organization_id, Message.conversation_id == conversation.id)
    if after is not None:
        rows = db.scalars(query.where(Message.seq > after).order_by(Message.seq).limit(limit + 1)).all()
        page = rows[:limit]
    else:
        if before is not None: query = query.where(Message.seq < before)
        rows = db.scalars(query.order_by(Message.seq.desc()).limit(limit + 1)).all()
        page = list(reversed(rows[:limit]))
    return {'messages': [message_view(row, user) for row in page], 'has_more': len(rows) > limit}


def mark(db, user, conversation, seq):
    statement = insert(ReadPosition).values(organization_id=user.organization_id, conversation_id=conversation.id, user_id=user.id,
        shipper_id=user.shipper_id, driver_id=user.driver_id, last_read_seq=seq, updated_at=now())
    db.execute(statement.on_conflict_do_update(index_elements=['conversation_id', 'user_id'],
        set_={'last_read_seq': func.greatest(ReadPosition.last_read_seq, statement.excluded.last_read_seq), 'updated_at': now()}))


def read(db, user, identity, data):
    """Monotonic, so retries are naturally idempotent; never moves past the newest message."""
    conversation, _ = conversation_for(db, user, identity)
    top = db.scalar(select(func.max(Message.seq)).where(Message.organization_id == user.organization_id, Message.conversation_id == conversation.id))
    if top: mark(db, user, conversation, min(data.seq, top))
    return {'unread': total_unread(db, user)}


def open_conversation(db, user, order, kind, driver_id):
    shipper_id = None if kind == 'DISPATCH_DRIVER' else order.shipper_id
    db.execute(insert(Conversation).values(id=uuid4(), organization_id=user.organization_id, order_id=order.id, kind=kind,
        shipper_id=shipper_id, driver_id=driver_id, created_at=now()).on_conflict_do_nothing(constraint='conversations_order_kind_driver'))
    return db.scalar(select(Conversation).where(Conversation.organization_id == user.organization_id, Conversation.order_id == order.id,
        Conversation.kind == kind, Conversation.driver_id.is_not_distinct_from(driver_id)))


def sender_name(db, user, order):
    if user.role == 'DRIVER':
        return db.scalar(select(Driver.name).where(Driver.organization_id == user.organization_id, Driver.id == user.driver_id)) or 'Driver'
    if user.role == 'SHIPPER': return shipper_name(order)
    return (user.display_name or DISPATCH)[:160]


def send(db, user, order_id, kind, data, key):
    person(user)
    def run():
        order, current = order_for(db, user, order_id)
        if kind not in ROLE_KINDS[user.role]: raise HTTPException(404, 'Chat not found.')
        driver_id = None if kind == 'DISPATCH_SHIPPER' else current
        reason = closed_reason(user, kind, order, driver_id, current)
        if reason: raise HTTPException(409, reason)
        conversation = open_conversation(db, user, order, kind, driver_id)
        row = Message(id=uuid4(), organization_id=user.organization_id, conversation_id=conversation.id, shipper_id=conversation.shipper_id,
            driver_id=conversation.driver_id, sender_user_id=user.id, sender_role=user.role, sender_name=sender_name(db, user, order),
            body=data.body, created_at=now())
        db.add(row); db.flush(); db.refresh(row)
        conversation.last_message_at = row.created_at
        mark(db, user, conversation, row.seq)
        audit(db, user, 'message.created', row.id, user.organization_id)
        return message_view(row, user)
    return command(db, user, key, f'chat-message:{order_id}:{kind}', data.model_dump(), run)


def inbox(db, user, limit=100):
    """Conversations with messages, newest first, for the Messages page and per-order badges."""
    person(user)
    with scope(db, user):
        rows = db.execute(visible(user, Conversation, Order).where(Conversation.last_message_at.is_not(None))
            .order_by(Conversation.last_message_at.desc(), Conversation.id).limit(limit)).all()
    if not rows: return []
    ids = [conversation.id for conversation, _ in rows]
    counts = unread(db, user, ids)
    last = {row.conversation_id: row for row in db.scalars(select(Message).where(Message.organization_id == user.organization_id,
        Message.conversation_id.in_(ids)).distinct(Message.conversation_id).order_by(Message.conversation_id, Message.seq.desc()))}
    route_ids = {order.route_id for _, order in rows if order.route_id}
    with company_scope(db, user):
        currents = dict(db.execute(select(Route.id, Route.driver_id).where(Route.organization_id == user.organization_id, Route.id.in_(route_ids))).all()) if route_ids else {}
    names = driver_names(db, user, [conversation.driver_id for conversation, _ in rows])
    result = []
    for conversation, order in rows:
        reason = closed_reason(user, conversation.kind, order, conversation.driver_id, currents.get(order.route_id))
        message = last.get(conversation.id)
        result.append(dict(conversation_id=conversation.id, order_id=order.id, order_number=order.number, order_status=order.status,
            kind=conversation.kind, title=title(user, conversation.kind, order, names.get(conversation.driver_id)), can_send=reason is None,
            unread=counts.get(conversation.id, 0), last_message=message_view(message, user) if message else None,
            last_message_at=conversation.last_message_at))
    return result
