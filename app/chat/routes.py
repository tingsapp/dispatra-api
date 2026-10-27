"""Order chat endpoints for dispatchers, Shippers and drivers (web session or native driver token)."""
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import service
from .schemas import ChannelView, ChatKind, InboxItem, MessageCreate, MessagePage, MessageView, ReadMark, ReadView

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Order chat'])


@router.get('/chats', response_model=list[InboxItem])
def inbox(slug: str, db: DB, limit: int = Query(100, ge=1, le=200), user: User = Depends(auth.company_user)):
    return service.inbox(db, user, limit)


@router.get('/orders/{order_id}/chats', response_model=list[ChannelView])
def order_chats(slug: str, order_id: UUID, db: DB, user: User = Depends(auth.company_user)):
    return service.channels(db, user, order_id)


@router.post('/orders/{order_id}/chats/{kind}/messages', response_model=MessageView, status_code=201)
def send(slug: str, order_id: UUID, kind: ChatKind, data: MessageCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    return service.send(db, user, order_id, kind, data, idempotency_key)


@router.get('/chats/{conversation_id}/messages', response_model=MessagePage)
def messages(slug: str, conversation_id: UUID, db: DB, after: int | None = Query(None, ge=0), before: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1, le=200), user: User = Depends(auth.company_user)):
    return service.messages(db, user, conversation_id, after, before, limit)


@router.post('/chats/{conversation_id}/read', response_model=ReadView)
def read(slug: str, conversation_id: UUID, data: ReadMark, db: DB, user: User = Depends(auth.company_user)):
    return service.read(db, user, conversation_id, data)


@router.get('/drivers/{driver_id}/chat', response_model=ChannelView)
def company_chat(slug: str, driver_id: UUID, db: DB, user: User = Depends(auth.company_user)):
    from . import company
    return company.channel(db, user, driver_id)


@router.post('/drivers/{driver_id}/chat/messages', response_model=MessageView, status_code=201)
def company_message(slug: str, driver_id: UUID, data: MessageCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    from . import company
    return company.send(db, user, driver_id, data, idempotency_key)


@router.get('/shippers/{shipper_id}/chat', response_model=ChannelView)
def shipper_company_chat(slug: str, shipper_id: UUID, db: DB, user: User = Depends(auth.company_user)):
    from . import company
    return company.channel(db, user, shipper_id, kind='shipper')


@router.post('/shippers/{shipper_id}/chat/messages', response_model=MessageView, status_code=201)
def shipper_company_message(slug: str, shipper_id: UUID, data: MessageCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    from . import company
    return company.send(db, user, shipper_id, data, idempotency_key, kind='shipper')
