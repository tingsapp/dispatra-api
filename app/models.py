from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from sqlalchemy import String, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Date, DateTime, Text, Integer, Numeric, Index, text, and_, or_, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

def now(): return datetime.now(timezone.utc)

class SubscriptionPlan(Base):
    __tablename__ = 'subscription_plans'
    __table_args__ = (
        CheckConstraint("id = '8eb730cb-226b-4ec1-a3fc-132279938d62'"),
        CheckConstraint('monthly_price >= 0'),
        CheckConstraint("currency ~ '^[A-Z]{3}$'"),
        CheckConstraint('version >= 1'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    monthly_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class CompanySubscription(Base):
    __tablename__ = 'company_subscriptions'
    __table_args__ = (
        CheckConstraint("status IN ('NOT_SUBSCRIBED', 'ACTIVE', 'PAUSED', 'CANCELLED')"),
        CheckConstraint('monthly_price >= 0'),
        CheckConstraint("currency ~ '^[A-Z]{3}$'"),
        CheckConstraint("status <> 'ACTIVE' OR (monthly_price IS NOT NULL AND starts_on IS NOT NULL)"),
        CheckConstraint('version >= 1'),
    )
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default='NOT_SUBSCRIBED')
    monthly_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    starts_on: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Organization(Base):
    __tablename__ = 'organizations'
    __table_args__ = (CheckConstraint("(trial_started_at IS NULL AND trial_ends_at IS NULL AND trial_activated_at IS NULL) OR (trial_started_at IS NOT NULL AND trial_ends_at IS NOT NULL AND trial_ends_at > trial_started_at AND (trial_activated_at IS NULL OR trial_activated_at >= trial_started_at))", name='trial_dates'),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    _active: Mapped[bool] = mapped_column('active', default=True)
    verification_pending: Mapped[bool] = mapped_column(default=False, server_default=text('false'))
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @hybrid_property
    def active(self):
        return bool(self._active) and not self.verification_pending and (self.trial_ends_at is None or self.trial_activated_at is not None or self.trial_ends_at > now())

    @active.setter
    def active(self, value):
        self._active = value
        if value and self.trial_ends_at is not None and self.trial_ends_at <= now() and self.trial_activated_at is None:
            self.trial_activated_at = now()

    @active.expression
    def active(cls):
        # Every SQL consumer (workers, API keys, webhooks and owner filters)
        # observes the same cutoff even when no expiry worker is running.
        return and_(cls._active, cls.verification_pending.is_(False), or_(cls.trial_ends_at.is_(None), cls.trial_activated_at.is_not(None), cls.trial_ends_at > func.now()))

    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('organization_id', 'id', name='verification_user_scope'),
        Index('one_active_company_dispatcher', 'organization_id', unique=True,
              postgresql_where=text("role = 'DISPATCHER' AND active")),
        Index('users_role_login_unique', 'role', text('lower(login_id)'), unique=True),
        Index('users_role_phone_unique', 'role', text('account_phone(phone)'), unique=True,
              postgresql_where=text('account_phone(phone) IS NOT NULL')),
        UniqueConstraint('shipper_id'), UniqueConstraint('driver_id'),
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
    phone: Mapped[str] = mapped_column(String(50), default='')
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

class EmailVerification(Base):
    __tablename__ = 'email_verifications'
    __table_args__ = (ForeignKeyConstraint(['organization_id', 'user_id'], ['users.organization_id', 'users.id']),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'))
    user_id: Mapped[UUID] = mapped_column()
    email: Mapped[str] = mapped_column(String(254))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    code_hash: Mapped[str] = mapped_column(Text)
    credential_hash: Mapped[str] = mapped_column(Text)
    secret: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resend_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(String(16), default='PENDING')

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
