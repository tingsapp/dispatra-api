"""Dedicated admin endpoints never expose driver or shipper conversations."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import inbox, service, support
from .schemas import ChannelView, InboxItem, InboxPage, ContactPage, MessageCreate, MessagePage, MessageView, ReadMark, ReadView

router = APIRouter(prefix='/api/v1/platform', tags=['Admin chat'])
Owner = Annotated[User, Depends(auth.platform)]

@router.get('/chats', response_model=InboxPage)
def chats(db: DB, user: Owner, cursor: str | None = Query(None, max_length=200), search: str = Query('', max_length=200), unread_only: bool = False, limit: int = Query(50, ge=1, le=100)):
    return inbox.page(db, user, limit, cursor, search, unread_only=unread_only)

@router.get('/chat-contacts', response_model=ContactPage)
def contacts(db: DB, user: Owner, search: str = Query('', max_length=200), cursor: UUID | None = None, limit: int = Query(50, ge=1, le=100)):
    return support.contacts(db, user, search, cursor, limit)

@router.get('/chat-unread', response_model=ReadView)
def unread(db: DB, user: Owner):
    return {'unread': service.total_unread(db, user)}

@router.get('/dispatchers/{dispatcher_id}/chat', response_model=ChannelView)
def channel(dispatcher_id: UUID, db: DB, user: Owner):
    return support.channel(db, user, dispatcher_id)

@router.post('/dispatchers/{dispatcher_id}/chat/messages', response_model=MessageView, status_code=201)
def send(dispatcher_id: UUID, data: MessageCreate, db: DB, user: Owner, idempotency_key: OperationKey):
    return support.send(db, user, dispatcher_id, data, idempotency_key)

@router.get('/chats/{identity}', response_model=InboxItem)
def detail(identity: UUID, db: DB, user: Owner):
    return inbox.detail(db, user, identity)

@router.get('/chats/{identity}/messages', response_model=MessagePage)
def messages(identity: UUID, db: DB, user: Owner, before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200)):
    return service.messages(db, user, identity, before=before, limit=limit)

@router.post('/chats/{identity}/read', response_model=ReadView)
def read(identity: UUID, data: ReadMark, db: DB, user: Owner):
    return service.read(db, user, identity, data)
