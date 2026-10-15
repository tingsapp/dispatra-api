from datetime import datetime
from uuid import UUID
from sqlalchemy import ForeignKey, String, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, now


class CompanyBilling(Base):
    __tablename__ = 'company_billing'
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    stripe_customer: Mapped[str | None] = mapped_column(String(255), unique=True)
    payment_method: Mapped[str | None] = mapped_column(String(255))
    brand: Mapped[str | None] = mapped_column(String(40))
    last4: Mapped[str | None] = mapped_column(String(4))
    exp_month: Mapped[int | None]
    exp_year: Mapped[int | None]
    setup_session: Mapped[str | None] = mapped_column(String(255))
    completed_session: Mapped[str | None] = mapped_column(String(255))
    setup_nonce: Mapped[str | None] = mapped_column(String(64))
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_terms: Mapped[dict | None] = mapped_column(JSONB)
    setup_terms_version: Mapped[int | None]
    stripe_subscription: Mapped[str | None] = mapped_column(String(255), unique=True)
    subscription_status: Mapped[str | None] = mapped_column(String(40))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoice_url: Mapped[str | None] = mapped_column(String(2048))
    pending: Mapped[dict | None] = mapped_column(JSONB)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restart_requested: Mapped[bool] = mapped_column(default=False, server_default='false')
    sync_error: Mapped[bool] = mapped_column(default=False, server_default='false')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
