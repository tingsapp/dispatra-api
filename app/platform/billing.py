"""Public SaaS plan terms and owner-only configuration; no charge is issued here."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.models import SubscriptionPlan, now
from app.schemas import Input
from app.services import audit, once
from app.subscription import MONTHLY_PLAN_ID, TRIAL_DAYS
from app.routes import DB, OperationKey
from .routes import Owner

Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class SubscriptionPlanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: Literal['Monthly subscription'] = 'Monthly subscription'
    interval: Literal['MONTHLY'] = 'MONTHLY'
    trial_enabled: bool = True
    trial_days: int = TRIAL_DAYS
    monthly_price: Decimal | None
    currency: str
    dispatch_monthly_price: Decimal | None = None
    dispatch_currency: str = 'CAD'


class MonthlyPlanView(SubscriptionPlanView):
    id: UUID
    version: int
    updated_at: datetime


class MonthlyPlanUpdate(Input):
    version: Annotated[int, Field(ge=1)]
    monthly_price: Price | None
    currency: Currency
    dispatch_monthly_price: Price | None = None
    dispatch_currency: Currency = 'CAD'
    trial_enabled: Annotated[bool, Field(strict=True)] = True
    trial_days: Annotated[int, Field(strict=True, ge=1, le=365)] = TRIAL_DAYS


def monthly_plan(db, lock=False):
    query = select(SubscriptionPlan).where(SubscriptionPlan.id == MONTHLY_PLAN_ID)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None: raise HTTPException(503, 'Subscription plan has not been initialized. Apply database migrations.')
    return row


def update_plan(db, actor, data, key):
    def run():
        row = monthly_plan(db, lock=True)
        if row.version != data.version: raise HTTPException(409, 'Subscription plan changed. Reload before saving.')
        row.monthly_price = data.monthly_price
        row.currency = data.currency
        for field in ('dispatch_monthly_price', 'dispatch_currency', 'trial_enabled', 'trial_days'):
            if field in data.model_fields_set: setattr(row, field, getattr(data, field))
        row.version += 1
        row.updated_at = now()
        audit(db, actor, 'subscription.plan_updated', row.id, None)
        db.flush()
        return MonthlyPlanView.model_validate(row).model_dump(mode='json')
    payload = data.model_dump(mode='json', exclude={'dispatch_monthly_price', 'dispatch_currency', 'trial_enabled', 'trial_days'})
    for field in ('dispatch_monthly_price', 'dispatch_currency', 'trial_enabled', 'trial_days'):
        if field in data.model_fields_set: payload[field] = data.model_dump(mode='json')[field]
    return once(db, actor, key, 'subscription-plan-update', payload, run)


router = APIRouter(prefix='/api/v1/platform/billing', tags=['Platform subscription and billing'])
public_router = APIRouter(prefix='/api/v1/subscription', tags=['Public subscription plans'])


@public_router.get('/plan', response_model=SubscriptionPlanView)
def get_public_plan(db: DB):
    return monthly_plan(db)


@router.get('/plan', response_model=MonthlyPlanView)
def get_plan(db: DB, user: Owner):
    return monthly_plan(db)


@router.patch('/plan', response_model=MonthlyPlanView)
def save_plan(data: MonthlyPlanUpdate, db: DB, idempotency_key: OperationKey, user: Owner):
    return update_plan(db, user, data, idempotency_key)
