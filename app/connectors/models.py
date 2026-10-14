from datetime import datetime
from uuid import UUID
from sqlalchemy import DateTime, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
from app.operations.models import TenantRecord

class Connection(TenantRecord, Base):
    __tablename__ = 'connector_connections'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'provider'))
    provider: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='CONNECTING')
    credentials: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String(200))
    account_label: Mapped[str | None] = mapped_column(String(160))
    profile_revision: Mapped[str | None] = mapped_column(String(80))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    lease: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_cursor: Mapped[str | None] = mapped_column(String(42))

class Authorization(TenantRecord, Base):
    __tablename__ = 'connector_authorizations'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('state_hash'))
    provider: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[UUID]
    state_hash: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(Text)
    connection_version: Mapped[int | None]
    profile_revision: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(default=False)

class Link(TenantRecord, Base):
    __tablename__ = 'connector_links'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('connection_id', 'kind', 'external_id'),
        ForeignKeyConstraint(['organization_id','connection_id'], ['connector_connections.organization_id','connector_connections.id']))
    connection_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(10))
    external_id: Mapped[str] = mapped_column(String(200))
    record_id: Mapped[UUID]
    fingerprint: Mapped[str | None] = mapped_column(String(64))

class Inbox(TenantRecord, Base):
    __tablename__ = 'connector_inbox'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('connection_id','kind','external_id','revision'),
        ForeignKeyConstraint(['organization_id','connection_id'], ['connector_connections.organization_id','connector_connections.id']),
        ForeignKeyConstraint(['organization_id','order_id'], ['orders.organization_id','orders.id']))
    connection_id: Mapped[UUID]
    external_id: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(String(64))
    profile_revision: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10), default='ORDER')
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default='RECEIVED')
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    order_id: Mapped[UUID | None]
