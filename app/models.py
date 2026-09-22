from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import String, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

def now(): return datetime.now(timezone.utc)

class Organization(Base):
    __tablename__ = 'organizations'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Customer(Base):
    __tablename__ = 'customers'
    __table_args__ = (UniqueConstraint('organization_id','number'), UniqueConstraint('organization_id','id'),
                     CheckConstraint("status IN ('ACTIVE','INACTIVE','ON_HOLD')"))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'), index=True)
    number: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(160))
    contact_name: Mapped[str] = mapped_column(String(160), default='')
    email: Mapped[str] = mapped_column(String(254), default='')
    phone: Mapped[str] = mapped_column(String(50), default='')
    address: Mapped[str] = mapped_column(String(500), default='')
    status: Mapped[str] = mapped_column(String(20), default='ACTIVE')
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('scope','login_id'), UniqueConstraint('customer_id'),
        ForeignKeyConstraint(['organization_id','customer_id'], ['customers.organization_id','customers.id']),
        CheckConstraint("(role = 'PLATFORM_OWNER' AND organization_id IS NULL AND customer_id IS NULL AND scope = 'platform') OR (role = 'DISPATCHER' AND organization_id IS NOT NULL AND customer_id IS NULL AND scope = organization_id::text) OR (role = 'CUSTOMER' AND organization_id IS NOT NULL AND customer_id IS NOT NULL AND scope = organization_id::text)"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'), index=True)
    customer_id: Mapped[UUID | None]
    scope: Mapped[str] = mapped_column(String(40))
    login_id: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), index=True)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Operation(Base):
    __tablename__ = 'operations'
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'))
    payload_hash: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSONB)

class LoginBucket(Base):
    __tablename__ = 'login_buckets'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int]
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class OutboxEvent(Base):
    __tablename__ = 'outbox_events'
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
