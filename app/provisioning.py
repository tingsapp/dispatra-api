"""One company/dispatcher/default-catalogue transaction for owner creation and public trials."""
from uuid import uuid4
from app.database import context
from app.models import CompanySubscription, Organization, SubscriptionPlan, User
from app.security import hash_password
from app.services import audit
from app.subscription import MONTHLY_PLAN_ID, TRIAL_DAYS


def provision_company(db, data, actor=None, organization_id=None, trial_start=None, pending_verification=False):
    from datetime import timedelta
    from app.operations.models import Settings
    from app.operations.seeding import seed_pricing
    from sqlalchemy import select
    org = Organization(id=organization_id or uuid4(), slug=data.slug, name=data.name, verification_pending=pending_verification,
        trial_started_at=trial_start, trial_ends_at=trial_start + timedelta(days=TRIAL_DAYS) if trial_start else None)
    db.add(org); db.flush()
    # Public registration receives only the freshly generated company's RLS scope.
    # It never uses the platform context or accepts an existing organization ID.
    if actor is None: context(db, org.id)
    plan = db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.id == MONTHLY_PLAN_ID))
    db.add(CompanySubscription(organization_id=org.id, monthly_price=plan.monthly_price, currency=plan.currency))
    account = User(organization_id=org.id, scope=str(org.id), login_id=data.admin_login,
        display_name=data.admin_display_name, phone=data.phone, password_hash=hash_password(data.password), role='DISPATCHER')
    db.add(account); db.flush()
    actor = actor or account
    seed_pricing(db, actor, org)
    settings = db.scalar(select(Settings).where(Settings.organization_id == org.id))
    settings.data = {**settings.data, 'phone': data.phone, 'address': data.address or None, 'tms_workspace': data.tms_workspace,
        **({'contact_name': data.admin_display_name} if trial_start or pending_verification else {})}
    if data.tms_workspace:
        from app.operations.tms_pricing import apply_workspace_rate
        apply_workspace_rate(db, actor, settings, True)
    audit(db, actor, 'organization.created', org.id, org.id)
    audit(db, actor, 'dispatcher.created', account.id, org.id)
    if trial_start: audit(db, actor, 'organization.trial_started', org.id, org.id)
    db.flush()
    return org, account
