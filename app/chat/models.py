"""Order chat records (migration 0033). Participant columns drive Shipper/driver RLS."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    order_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(20))
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = 'conversation_messages'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    conversation_id: Mapped[UUID]
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    sender_user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    sender_role: Mapped[str] = mapped_column(String(12))
    sender_name: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReadPosition(Base):
    __tablename__ = 'conversation_reads'
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    conversation_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    last_read_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
