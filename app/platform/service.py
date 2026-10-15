"""Platform-owner commands. Every query is explicitly scoped to one organization; the owner's
RLS platform context never substitutes for that scope."""
import secrets
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, delete, func
from app.models import Organization, User, LoginSession, now
from app.security import hash_password, rate_limit
from app.services import audit, once
from app.schemas import OrganizationView
from app.operations.common import company_lock
from . import queries


def company(db, organization_id, lock=False):
    query = select(Organization).where(Organization.id == organization_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None: raise HTTPException(404, 'Company not found.')
    return row


def expect(row, version, label):
    if row.version != version: raise HTTPException(409, f'{label} changed. Reload before continuing.')


def create_organization(db, actor, data, key):
    def run():
        from app.provisioning import provision_company
        organization, _ = provision_company(db, data, actor=actor)
        return OrganizationView.model_validate(organization).model_dump(mode='json')
    return once(db, actor, key, 'organization', company_payload(data), run)


def update_company(db, actor, organization_id, data, key):
    def run():
        serialize_lock(db, organization_id)
        org = company(db, organization_id, True); expect(org, data.version, 'Company')
        apply_company_details(db, org, data)
        org.version += 1; org.updated_at = now()
        audit(db, actor, 'organization.updated', org.id, org.id)
        db.flush()
        return queries.company_detail(db, org)
    result = once(db, actor, key, f'platform-company-update:{organization_id}', company_payload(data), run)
    return queries.include_company_details(db, organization_id, result)


def company_payload(data):
    # Preserve pre-contact request hashes on idempotent replays.
    payload = data.model_dump(mode='json')
    for field in ('phone', 'address', 'settings_version'):
        if field not in data.model_fields_set: payload.pop(field, None)
    if payload.get('tms_workspace') is False: payload.pop('tms_workspace')
    return payload


def apply_company_details(db, org, data):
    from app.operations.models import Settings
    settings = db.scalar(select(Settings).where(Settings.organization_id == org.id).with_for_update())
    contact = {field: getattr(data, field) for field in ('phone', 'address') if getattr(data, field) is not None}
    if contact:
        if not settings: raise HTTPException(409, 'Company settings have not been initialized. Run the pricing seeder.')
        if data.settings_version is None: raise HTTPException(422, 'Company settings version is required when editing phone or address.')
        expect(settings, data.settings_version, 'Company settings')
    changed = org.name != data.name
    org.name = data.name
    if settings:
        changes = {'company_name': data.name} if changed else {}
        if 'phone' in contact and settings.data.get('phone', '') != contact['phone']:
            changes['phone'] = contact['phone']
        if 'address' in contact:
            current = settings.data.get('address')
            text = current.get('text', '') if isinstance(current, dict) else current or ''
            if text != contact['address']: changes['address'] = contact['address'] or None
        if changes:
            settings.data = {**settings.data, **changes}
            settings.version += 1
            settings.updated_at = now()
            changed = True
    return changed


def apply_company_access(db, actor, org, active):
    expired_trial = org.trial_ends_at is not None and org.trial_activated_at is None and org.trial_ends_at <= now()
    org.active = active
    if not active or expired_trial:
        db.execute(delete(LoginSession).where(LoginSession.organization_id == org.id))
        close_open_duty(db, actor, org.id)
    audit(db, actor, 'organization.activated' if active else 'organization.suspended', org.id, org.id)


def set_company_status(db, actor, organization_id, active, data, key):
    """Suspension revokes every company session and closes open driver duty; historical records are untouched."""
    def run():
        org = company(db, organization_id, True); expect(org, data.version, 'Company')
        if org._active == active: raise HTTPException(409, 'Company is already ' + ('active.' if active else 'suspended.'))
        apply_company_access(db, actor, org, active)
        org.version += 1; org.updated_at = now()
        db.flush()
        return queries.company_detail(db, org)
    result = once(db, actor, key, f'platform-company-{"activate" if active else "suspend"}:{organization_id}', data.model_dump(), run)
    return queries.include_company_details(db, organization_id, result)


def close_open_duty(db, actor, organization_id):
    from app.operations.models import DutySession
    from app.operations.common import changed
    scoped = SimpleNamespace(id=actor.id, organization_id=organization_id)
    for duty in db.scalars(select(DutySession).where(DutySession.organization_id == organization_id, DutySession.ended_at.is_(None)).with_for_update()):
        duty.ended_at = now()
        changed(db, scoped, duty, 'duty.session_ended')


def dispatcher(db, organization_id, user_id, lock=False):
    query = select(User).where(User.id == user_id, User.organization_id == organization_id, User.role == 'DISPATCHER')
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None: raise HTTPException(404, 'Dispatcher not found.')
    return row


def serialize_lock(db, organization_id):
    # Serializes account changes per company so dispatcher limits cannot race.
    company_lock(db, SimpleNamespace(organization_id=organization_id))


def ensure_dispatcher_capacity(db, organization_id, activating=None):
    query = select(User.id).where(User.organization_id == organization_id, User.role == 'DISPATCHER')
    if activating is not None:
        query = query.where(User.active.is_(True), User.id != activating)
    if db.scalar(query.limit(1)):
        raise HTTPException(409, 'Only one dispatcher account is allowed per company.')


def unique_login(db, organization_id, login_id, current=None):
    query = select(User.id).where(User.role == 'DISPATCHER', func.lower(User.login_id) == login_id.lower())
    if current: query = query.where(User.id != current)
    if db.scalar(query): raise HTTPException(409, 'That email is already used by a dispatcher.')


def revoke(db, user_id):
    return db.execute(delete(LoginSession).where(LoginSession.user_id == user_id)).rowcount or 0


def touched(db, actor, account, action):
    account.version += 1; account.updated_at = now()
    audit(db, actor, action, account.id, account.organization_id)
    db.flush()
    return queries.dispatcher_view(db, account)


def create_dispatcher(db, actor, organization_id, data, key):
    password = None
    def run():
        nonlocal password
        serialize_lock(db, organization_id)
        org = company(db, organization_id)
        ensure_dispatcher_capacity(db, org.id)
        unique_login(db, org.id, data.login_id)
        password = secrets.token_urlsafe(18)
        account = User(organization_id=org.id, scope=str(org.id), login_id=data.login_id, display_name=data.display_name,
                       role='DISPATCHER', password_hash=hash_password(password))
        db.add(account); db.flush()
        audit(db, actor, 'dispatcher.created', account.id, org.id)
        db.flush()
        return queries.dispatcher_view(db, account)
    result = once(db, actor, key, f'platform-dispatcher-create:{organization_id}', data.model_dump(), run)
    return {**result, 'initial_password': password}


def update_dispatcher(db, actor, organization_id, user_id, data, key):
    def run():
        serialize_lock(db, organization_id)
        account = dispatcher(db, organization_id, user_id, True); expect(account, data.version, 'Dispatcher account')
        apply_dispatcher_details(db, account, data)
        return touched(db, actor, account, 'dispatcher.updated')
    return once(db, actor, key, f'platform-dispatcher-update:{user_id}', data.model_dump(), run)


def apply_dispatcher_details(db, account, data):
    if account.login_id != data.login_id: unique_login(db, account.organization_id, data.login_id, account.id)
    account.login_id = data.login_id; account.display_name = data.display_name


def set_dispatcher_active(db, actor, organization_id, user_id, active, data, key):
    def run():
        serialize_lock(db, organization_id)
        account = dispatcher(db, organization_id, user_id, True); expect(account, data.version, 'Dispatcher account')
        if account.active == active: raise HTTPException(409, 'Dispatcher account is already ' + ('active.' if active else 'deactivated.'))
        if active:
            ensure_dispatcher_capacity(db, organization_id, activating=account.id)
        if not active:
            others = db.scalar(select(func.count()).select_from(User).where(User.organization_id == organization_id,
                User.role == 'DISPATCHER', User.active.is_(True), User.id != account.id))
            if not others: raise HTTPException(409, 'Keep the company dispatcher active. Suspend the company to block workspace access.')
            revoke(db, account.id)
        account.active = active
        return touched(db, actor, account, 'dispatcher.activated' if active else 'dispatcher.deactivated')
    return once(db, actor, key, f'platform-dispatcher-{"activate" if active else "deactivate"}:{user_id}', data.model_dump(), run)


def reset_dispatcher_password(db, actor, organization_id, user_id, data, key):
    password = None
    def run():
        nonlocal password
        account = dispatcher(db, organization_id, user_id, True); expect(account, data.version, 'Dispatcher account')
        password = secrets.token_urlsafe(18)
        apply_dispatcher_password(db, actor, account, password)
        return touched(db, actor, account, 'dispatcher.password_reset')
    result = once(db, actor, key, f'platform-dispatcher-reset:{user_id}', data.model_dump(), run)
    return {**result, 'initial_password': password}


def apply_dispatcher_password(db, actor, account, password):
    rate_limit('platform-reset:' + str(actor.id), 30)
    account.password_hash = hash_password(password)
    revoke(db, account.id)


def revoke_dispatcher_sessions(db, actor, organization_id, user_id, key):
    def run():
        account = dispatcher(db, organization_id, user_id, True)
        revoked = revoke(db, account.id)
        audit(db, actor, 'dispatcher.sessions_revoked', account.id, organization_id)
        db.flush()
        return {'revoked': revoked}
    return once(db, actor, key, f'platform-dispatcher-revoke:{user_id}', {}, run)
