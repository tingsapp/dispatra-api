from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import String, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, DateTime, Text, Integer
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
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('scope','login_id'), UniqueConstraint('shipper_id'), UniqueConstraint('driver_id'),
        ForeignKeyConstraint(['organization_id','shipper_id'], ['shippers.organization_id','shippers.id']),
        ForeignKeyConstraint(['organization_id','driver_id'], ['drivers.organization_id','drivers.id']),
        CheckConstraint("(role = 'ADMIN' AND organization_id IS NULL AND shipper_id IS NULL AND driver_id IS NULL AND scope = 'platform') OR (role = 'DISPATCHER' AND organization_id IS NOT NULL AND shipper_id IS NULL AND driver_id IS NULL AND scope = organization_id::text) OR (role = 'SHIPPER' AND organization_id IS NOT NULL AND shipper_id IS NOT NULL AND driver_id IS NULL AND scope = organization_id::text) OR (role = 'DRIVER' AND organization_id IS NOT NULL AND driver_id IS NOT NULL AND shipper_id IS NULL AND scope = organization_id::text)", name="user_role_scope"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('organizations.id'), index=True)
    shipper_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    scope: Mapped[str] = mapped_column(String(40))
    login_id: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(default=True)
    display_name: Mapped[str] = mapped_column(String(160), default='')
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Account administration changes set this explicitly; sign-in only records last_login_at.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # NULL when the Dispatch agent acts in AUTO mode; the matching event carries actor type `AGENT`.
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Operation(Base):
    __tablename__ = 'operations'
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
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
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(80))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
