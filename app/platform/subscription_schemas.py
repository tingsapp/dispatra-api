from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, model_validator
from app.schemas import Input
from .billing import Price, Currency
from .schemas import Version

SubscriptionStatus = Literal['NOT_SUBSCRIBED', 'ACTIVE', 'PAUSED', 'CANCELLED']


class CompanySubscriptionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    organization_id: UUID
    status: SubscriptionStatus
    monthly_price: Decimal | None
    currency: str
    starts_on: date | None
    version: int
    updated_at: datetime


class CompanySubscriptionUpdate(Input):
    version: Version
    status: SubscriptionStatus
    monthly_price: Price | None
    currency: Currency
    starts_on: date | None

    @model_validator(mode='after')
    def active_terms(self):
        if self.status == 'ACTIVE' and (self.monthly_price is None or self.starts_on is None):
            raise ValueError('Active subscriptions require a monthly price and start date.')
        return self


class CompanySubscriptionSummary(CompanySubscriptionView):
    company_name: str
    company_slug: str
    workspace_active: bool
    trial_ends_at: datetime | None
    trial_activated_at: datetime | None
    contributing: bool


class RevenueCurrency(BaseModel):
    currency: str
    monthly_recurring_revenue: Decimal
    annualized_revenue: Decimal
    active_subscriptions: int


class PlatformRevenueView(BaseModel):
    as_of: date
    currencies: list[RevenueCurrency]
    total_companies: int
    active_subscriptions: int
    scheduled_subscriptions: int
    not_subscribed: int
    paused_subscriptions: int
    cancelled_subscriptions: int

