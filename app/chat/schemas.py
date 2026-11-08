from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, Field
from app.schemas import Input

ChatKind = Literal['DISPATCH_SHIPPER', 'DISPATCH_DRIVER', 'DRIVER_SHIPPER', 'ADMIN_DISPATCHER']
ChatRole = Literal['DISPATCHER', 'SHIPPER', 'DRIVER', 'ADMIN']


class MessageCreate(Input):
    expected_conversation_id: UUID | None = None
    expected_driver_id: UUID | None = None
    body: Annotated[str, Field(min_length=1, max_length=2000)]


class ReadMark(Input):
    seq: int = Field(ge=1)


class MessageView(BaseModel):
    id: UUID
    seq: int
    conversation_id: UUID
    sender_role: ChatRole
    sender_name: str
    mine: bool
    body: str
    created_at: datetime


class MessagePage(BaseModel):
    messages: list[MessageView]
    has_more: bool


class ChannelView(BaseModel):
    dispatcher_id: UUID | None = None
    company_name: str | None = None
    shipper_id: UUID | None = None
    """One chat the caller can open on an Order. `conversation_id` is null until the first message."""
    kind: ChatKind
    conversation_id: UUID | None
    title: str
    driver_id: UUID | None
    can_send: bool
    closed_reason: str | None
    unread: int
    last_message_at: datetime | None


class InboxItem(BaseModel):
    closed_reason: str | None = None
    dispatcher_id: UUID | None = None
    company_name: str | None = None
    shipper_id: UUID | None = None
    conversation_id: UUID
    order_id: UUID | None
    order_number: str | None
    order_status: str | None
    kind: ChatKind
    title: str
    can_send: bool
    unread: int
    last_message: MessageView | None
    last_message_at: datetime | None
    driver_id: UUID | None = None


class ReadView(BaseModel):
    unread: int
    notifications_unread: int | None = None


class InboxPage(BaseModel):
    items: list[InboxItem]
    next_cursor: str | None


class ChatContact(BaseModel):
    id: UUID
    name: str
    company_name: str


class ContactPage(BaseModel):
    items: list[ChatContact]
    next_cursor: UUID | None
