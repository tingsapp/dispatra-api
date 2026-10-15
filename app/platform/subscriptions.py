"""Owner-only company agreements and exact recurring-revenue projections."""
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select, func, case, and_, or_
from app.models import CompanySubscription, Organization, now
from app.operations.models import Vehicle
from app.services import audit, once
from .service import company, expect
from .queries import _escape
from .subscription_schemas import CompanySubscriptionView, CompanySubscriptionSummary, PlatformRevenueView, RevenueCurrency


def as_of():
    # Subscription dates are calendar dates in UTC, independent of host timezone.
    return datetime.now(timezone.utc).date()


def contributing(today):
    return and_(CompanySubscription.status == 'ACTIVE', CompanySubscription.starts_on <= today)


def subscription(db, organization_id, lock=False):
    company(db, organization_id)
    query = select(CompanySubscription).where(CompanySubscription.organization_id == organization_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None: raise HTTPException(503, 'Company subscription has not been initialized. Apply migrations or seed the company.')
    return row


def update_subscription(db, actor, organization_id, data, key):
    def run():
        company(db, organization_id, lock=True)
        row = subscription(db, organization_id, lock=True)
        expect(row, data.version, 'Company subscription')
        previous_status = row.status
        for field in ('status', 'monthly_price', 'currency', 'starts_on'):
            setattr(row, field, getattr(data, field))
        from app.company_billing.models import CompanyBilling
        from app.company_billing.recurring import request_sync
        billing = db.get(CompanyBilling, organization_id)
        if billing:
            if data.status == 'ACTIVE' and previous_status in {'PAUSED', 'CANCELLED', 'NOT_SUBSCRIBED'}:
                billing.restart_requested = True
            request_sync(billing)
        # Access is revoked in this transaction when an owner pauses/cancels.
        if data.status != 'ACTIVE': db.get(Organization, organization_id).billing_access_until = None
        row.version += 1
        row.updated_at = now()
        audit(db, actor, 'subscription.company_updated', organization_id, organization_id)
        db.flush()
        return CompanySubscriptionView.model_validate(row).model_dump(mode='json')
    return once(db, actor, key, f'company-subscription:{organization_id}', data.model_dump(mode='json'), run)


def subscriptions(db, search=None, status=None, after=None, limit=50):
    query = select(CompanySubscription, Organization, contributing(as_of())).join(Organization)
    if search:
        pattern = f'%{_escape(search.strip().lower())}%'
        query = query.where(or_(func.lower(Organization.name).like(pattern, escape='\\'), Organization.slug.like(pattern, escape='\\')))
    if status: query = query.where(CompanySubscription.status == status)
    if after: query = query.where(CompanySubscription.organization_id > after)
    query = query.order_by(CompanySubscription.organization_id).limit(limit)
    return [CompanySubscriptionSummary(
        **CompanySubscriptionView.model_validate(row).model_dump(), company_name=org.name, company_slug=org.slug,
        workspace_active=org.active, trial_ends_at=org.trial_ends_at, trial_activated_at=org.trial_activated_at,
        contributing=bool(included)) for row, org, included in db.execute(query)]


def revenue(db):
    today = as_of()
    active = contributing(today)
    # Aggregate all saved agreements, independently of the paginated display table.
    predicates = [active, and_(CompanySubscription.status == 'ACTIVE', CompanySubscription.starts_on > today),
        CompanySubscription.status == 'NOT_SUBSCRIBED', CompanySubscription.status == 'PAUSED', CompanySubscription.status == 'CANCELLED']
    # Amounts and counts share one statement snapshot even during an owner edit.
    fleet = select(Vehicle.organization_id, func.count().label('vehicle_count')).where(
        Vehicle.archived_at.is_(None)).group_by(Vehicle.organization_id).subquery()
    monthly_total = CompanySubscription.monthly_price * func.coalesce(fleet.c.vehicle_count, 0)
    grouped = db.execute(select(CompanySubscription.currency,
        func.sum(case((active, monthly_total), else_=0)), func.count(),
        *[func.count(case((predicate, 1))) for predicate in predicates])
        .outerjoin(fleet, fleet.c.organization_id == CompanySubscription.organization_id)
        .group_by(CompanySubscription.currency).order_by(CompanySubscription.currency)).all()
    counts = [sum(row[index] for row in grouped) for index in range(2, 8)]
    return PlatformRevenueView(as_of=today, currencies=[RevenueCurrency(currency=currency,
        monthly_recurring_revenue=Decimal(amount), annualized_revenue=Decimal(amount) * 12, active_subscriptions=active_count)
        for currency, amount, _, active_count, *_ in grouped if active_count], **dict(zip(('total_companies', 'active_subscriptions',
        'scheduled_subscriptions', 'not_subscribed', 'paused_subscriptions', 'cancelled_subscriptions'), counts)))

