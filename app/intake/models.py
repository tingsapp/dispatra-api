from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
from app.operations.models import TenantRecord

STATUSES = ('RECEIVED', 'ORDER_CREATED', 'NEEDS_REVIEW', 'UNKNOWN_SENDER', 'NOT_AN_ORDER', 'FAILED', 'DISCARDED')


class MailboxConnection(TenantRecord, Base):
    """One IMAP mailbox per company. `secret` holds only the encrypted app password."""
    __tablename__ = 'mailbox_connections'
    __table_args__ = (UniqueConstraint('organization_id'), UniqueConstraint('organization_id', 'id'),
        ForeignKeyConstraint(['organization_id', 'default_service_id'], ['catalog_entries.organization_id', 'catalog_entries.id']))
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int]
    username: Mapped[str] = mapped_column(String(254))
    secret: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(String(120), default='INBOX')
    enabled: Mapped[bool] = mapped_column(default=True)
    default_service_id: Mapped[UUID | None]
    uid_validity: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))


class EmailIntake(TenantRecord, Base):
    __tablename__ = 'email_intakes'
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'message_id', name='email_intakes_message'),
        ForeignKeyConstraint(['organization_id', 'shipper_id'], ['shippers.organization_id', 'shippers.id']),
        ForeignKeyConstraint(['organization_id', 'order_id'], ['orders.organization_id', 'orders.id']),
        CheckConstraint(f"status IN {STATUSES}", name='email_intake_status'))
    message_id: Mapped[str] = mapped_column(String(998))
    mailbox_uid: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_address: Mapped[str] = mapped_column(String(254))
    from_name: Mapped[str] = mapped_column(String(160), default='')
    subject: Mapped[str] = mapped_column(String(500), default='')
    body: Mapped[str] = mapped_column(Text)
    sender_verified: Mapped[bool] = mapped_column(default=False)
    shipper_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(20), default='RECEIVED')
    extraction: Mapped[dict | None] = mapped_column(JSONB)
    draft: Mapped[dict | None] = mapped_column(JSONB)
    missing: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(String(500), default='')
    order_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    attempts: Mapped[int] = mapped_column(default=0)
