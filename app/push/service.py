"""Driver push: device registration and queueing inside the transaction that created the driver's update.

Pushes mirror the driver's inbox notifications (assignment, route release, dispatcher duty changes) and
new chat messages from dispatch or a Shipper. Text names the Order number or sender only: no addresses,
contact details, prices or chat text. A push never starts location sharing; the app refetches on open.
"""
from uuid import uuid4
from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from app.models import LoginSession, OutboxEvent, User, now
from app.operations.orders import company_scope
from .models import PushDevice, PushMessage

EVENT_TYPE = 'push.requested'


def register(db, user, session_hash, data):
    """Bind this phone's token to the signed-in driver session; a token re-registered by another driver moves to them."""
    statement = insert(PushDevice).values(id=uuid4(), organization_id=user.organization_id, user_id=user.id, driver_id=user.driver_id,
        session_hash=session_hash, platform=data.platform, token=data.token, created_at=now(), last_seen_at=now())
    statement = statement.on_conflict_do_update(constraint='push_devices_token', set_={'user_id': user.id, 'driver_id': user.driver_id,
        'session_hash': session_hash, 'platform': data.platform, 'last_seen_at': now()})
    # The previous owner's row is invisible to this driver's RLS scope; the upsert targets only this token.
    with company_scope(db, user): db.execute(statement)


def unregister(db, user, data):
    db.execute(delete(PushDevice).where(PushDevice.organization_id == user.organization_id, PushDevice.user_id == user.id,
        PushDevice.token == data.token))


def live_devices(organization_id, user_id):
    return (select(PushDevice).join(LoginSession, LoginSession.token_hash == PushDevice.session_hash)
        .where(PushDevice.organization_id == organization_id, PushDevice.user_id == user_id, LoginSession.expires_at > now()))


def queue(db, organization_id, user, kind, title, body, data):
    """Queue one push for a driver account that has a signed-in phone. Call inside the producing transaction."""
    if not user or not user.driver_id or not user.active: return
    if not db.scalar(select(exists(live_devices(organization_id, user.id).subquery()))): return
    row = PushMessage(id=uuid4(), organization_id=organization_id, user_id=user.id, driver_id=user.driver_id, kind=kind,
        title=title[:160], body=body[:500], data={key: str(value) for key, value in data.items() if value is not None}, created_at=now())
    db.add(row)
    db.add(OutboxEvent(organization_id=organization_id, event_type=EVENT_TYPE, entity_id=row.id))


def for_notification(db, notification, user):
    queue(db, notification.organization_id, user, notification.kind, notification.title, notification.body,
        {'type': 'notification', 'notification_id': notification.id, 'order_id': notification.order_id})


def for_chat_message(db, organization_id, message_id):
    """A dispatcher or Shipper message reaches the conversation's driver; the driver's own messages never push."""
    from app.chat.models import Conversation, Message
    from app.operations.models import Order
    message = db.get(Message, message_id)
    if message is None or message.sender_role == 'DRIVER': return
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.driver_id is None: return
    order = db.get(Order, conversation.order_id)
    driver = db.scalar(select(User).where(User.organization_id == organization_id, User.driver_id == conversation.driver_id, User.active.is_(True)))
    title = 'Message from Dispatch' if message.sender_role == 'DISPATCHER' else f'Message from {message.sender_name}'
    queue(db, organization_id, driver, 'message.created', title, f'{order.number if order else "Your order"}: new message',
        {'type': 'chat', 'order_id': conversation.order_id, 'conversation_id': conversation.id})
