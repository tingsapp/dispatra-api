from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, Field
from app.schemas import Input


class CompanyPaymentView(BaseModel):
    status: Literal['MISSING', 'SAVED', 'EXPIRED']
    required: bool
    trial_ends_at: datetime | None
    setup_available: bool
    monthly_price: Decimal | None = None
    currency: str = 'CAD'
    vehicle_count: int = 0
    monthly_total: Decimal | None = None
    terms_version: int = 1
    starts_at: datetime | None = None
    subscription_status: str = 'NOT_SUBSCRIBED'
    period_end: datetime | None = None
    invoice_url: str | None = None
    sync_error: bool = False
    brand: str | None = None
    last4: str | None = None
    exp_month: int | None = None
    exp_year: int | None = None


class CardSetupView(BaseModel):
    url: str


class CardSetupConfirm(Input):
    session_id: Annotated[str, Field(pattern=r'^cs_[a-zA-Z0-9_]+$', max_length=255)]


class CardSetupRequest(Input):
    consent: Literal[True]
    terms_version: Annotated[int, Field(ge=1)]


class CompanyPaymentEntry(BaseModel):
    id: str
    created_at: datetime
    amount: Decimal
    refunded_amount: Decimal
    currency: str
    status: Literal['PAID', 'PENDING', 'FAILED', 'REFUNDED', 'PARTIALLY_REFUNDED']
    receipt_url: str | None


class CompanyPaymentHistory(BaseModel):
    available: bool
    items: list[CompanyPaymentEntry]
    next_cursor: str | None
