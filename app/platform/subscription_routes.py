from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Query
from app.routes import DB, OperationKey
from .routes import Owner
from . import subscriptions
from .subscription_schemas import CompanySubscriptionView, CompanySubscriptionUpdate, CompanySubscriptionSummary, PlatformRevenueView, SubscriptionStatus

router = APIRouter(prefix='/api/v1/platform', tags=['Platform company subscriptions and revenue'])


@router.get('/organizations/{organization_id}/subscription', response_model=CompanySubscriptionView)
def get_subscription(organization_id: UUID, db: DB, user: Owner):
    return subscriptions.subscription(db, organization_id)


@router.patch('/organizations/{organization_id}/subscription', response_model=CompanySubscriptionView)
def save_subscription(organization_id: UUID, data: CompanySubscriptionUpdate, db: DB, idempotency_key: OperationKey, user: Owner):
    return subscriptions.update_subscription(db, user, organization_id, data, idempotency_key)


@router.get('/billing/subscriptions', response_model=list[CompanySubscriptionSummary])
def list_subscriptions(db: DB, user: Owner, search: Annotated[str | None, Query(max_length=100)] = None,
    status: SubscriptionStatus | None = None, after: UUID | None = None, limit: int = Query(50, ge=1, le=100)):
    return subscriptions.subscriptions(db, search, status, after, limit)


@router.get('/billing/revenue', response_model=PlatformRevenueView)
def get_revenue(db: DB, user: Owner):
    return subscriptions.revenue(db)
