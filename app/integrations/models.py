from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now
from app.operations.models import TenantRecord

LINK_KINDS = ('DRIVER', 'VEHICLE', 'SHIPPER', 'ORDER')
INBOX_STATUSES = ('RECEIVED', 'ORDER_CREATED', 'ORDER_UPDATED', 'ORDER_CANCELLED', 'NEEDS_REVIEW', 'FAILED', 'IGNORED')


class TmsConnection(TenantRecord, Base):
    """One external TMS per company. `credential` and `webhook_secret` hold only encrypted values."""
    __tablename__ = 'tms_connections'
    __table_args__ = (UniqueConstraint('organization_id'), UniqueConstraint('organization_id', 'id'),
        CheckConstraint("auth_type IN ('api_key','bearer')", name='tms_auth_type'),
        CheckConstraint("sync_method IN ('webhook','polling')", name='tms_sync_method'),
        CheckConstraint('fleet_sync_minutes IN (15, 60, 360, 1440)', name='tms_fleet_interval'),
        CheckConstraint('poll_interval_minutes IN (1, 5, 15)', name='tms_poll_interval'))
    enabled: Mapped[bool] = mapped_column(default=True)
    base_url: Mapped[str] = mapped_column(String(2048))
    auth_type: Mapped[str] = mapped_column(String(10))
    credential: Mapped[str] = mapped_column(Text)
    drivers_endpoint: Mapped[str] = mapped_column(String(500))
    vehicles_endpoint: Mapped[str] = mapped_column(String(500))
    fleet_sync_minutes: Mapped[int] = mapped_column(default=60)
    sync_method: Mapped[str] = mapped_column(String(10))
    webhook_secret: Mapped[str | None] = mapped_column(Text)
    orders_endpoint: Mapped[str] = mapped_column(String(500))
    poll_interval_minutes: Mapped[int] = mapped_column(default=5)
    status_endpoint: Mapped[str] = mapped_column(String(500))
    field_maps: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_fleet_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fleet_result: Mapped[dict | None] = mapped_column(JSONB)
    last_order_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_error: Mapped[str | None] = mapped_column(String(80))


class TmsLink(TenantRecord, Base):
    """A Dispatra record imported from the TMS, keyed by the TMS's own identifier."""
    __tablename__ = 'tms_links'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'kind', 'external_id', name='tms_links_external'),
        UniqueConstraint('organization_id', 'kind', 'record_id', name='tms_links_record'),
        CheckConstraint(f'kind IN {LINK_KINDS}', name='tms_link_kind'))
    kind: Mapped[str] = mapped_column(String(10))
    external_id: Mapped[str] = mapped_column(String(200))
    record_id: Mapped[UUID]
    fingerprint: Mapped[str | None] = mapped_column(String(64))


class TmsInbox(TenantRecord, Base):
    """One received TMS order payload, stored before processing and deduplicated by TMS order ID and revision."""
    __tablename__ = 'tms_inbox'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'external_id', 'revision', name='tms_inbox_revision'),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']),
        CheckConstraint(f'status IN {INBOX_STATUSES}', name='tms_inbox_status'),
        CheckConstraint("received_via IN ('WEBHOOK','POLLING')", name='tms_inbox_via'))
    external_id: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(String(200))
    event: Mapped[str] = mapped_column(String(40), default='order.updated')
    received_via: Mapped[str] = mapped_column(String(10))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default='RECEIVED')
    missing: Mapped[list] = mapped_column(JSONB, default=list)
    order_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    attempts: Mapped[int] = mapped_column(default=0)


class ApiWebhook(TenantRecord, Base):
    """The company's signed webhook for Dispatra API order events. `secret` holds only the encrypted signing secret."""
    __tablename__ = 'api_webhooks'
    __table_args__ = (UniqueConstraint('organization_id'), UniqueConstraint('organization_id', 'id'))
    url: Mapped[str] = mapped_column(String(2048))
    secret: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))


class ApiKey(TenantRecord, Base):
    """A company's Dispatra API key. Only the SHA-256 hash and a display prefix are stored."""
    __tablename__ = 'api_keys'
    __table_args__ = (UniqueConstraint('organization_id', 'id'),)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
