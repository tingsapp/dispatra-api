"""Append-only company event log and per-recipient notification inbox (migration 0020)."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Identity, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class Event(Base):
    """One committed change. `tx` (xid8, database default) is not mapped; feed queries read it directly."""
    __tablename__ = 'events'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        CheckConstraint("actor_type IN ('USER','SYSTEM','AGENT')", name='event_actor_type'))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    type: Mapped[str] = mapped_column(String(80))
    actor_type: Mapped[str] = mapped_column(String(10), default='USER')
    actor_id: Mapped[UUID | None]
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[UUID]
    entity_version: Mapped[int | None]
    order_id: Mapped[UUID | None]
    route_id: Mapped[UUID | None]
    # Audience: company dispatchers, one Shipper, one driver and/or one recipient account.
    dispatchers: Mapped[bool] = mapped_column(default=True)
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    user_id: Mapped[UUID | None]
    correlation_id: Mapped[UUID]
    causation_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Notification(Base):
    __tablename__ = 'notifications'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),
        UniqueConstraint('organization_id', 'recipient_user_id', 'dedupe_key'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']),
        ForeignKeyConstraint(['organization_id', 'route_id'], ['routes.organization_id', 'routes.id']),
        CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name='notification_severity'))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    recipient_user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    # Recipient scope copied from the account so Shipper/driver RLS can narrow rows.
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    event_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(10), default='INFO')
    order_id: Mapped[UUID | None]
    route_id: Mapped[UUID | None]
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String(500))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class EventSubscription(Base):
    """Independent server-side consumer position in one company's event log."""
    __tablename__ = 'event_subscriptions'
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
    position: Mapped[str] = mapped_column(String(42))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
