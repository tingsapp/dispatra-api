"""Public trial registration: fixed dispatcher role, isolated new tenant, retry-safe credentials."""
from types import SimpleNamespace
from uuid import UUID, uuid5, NAMESPACE_URL
from sqlalchemy import delete
from fastapi import HTTPException
from app import auth
from app.database import context
from app.models import User, Organization, LoginSession, now
from app.provisioning import provision_company
from app.schemas import OrganizationCreate
from app.security import digest, rate_limit, verify
from app.services import once, audit


def start_trial(db, data, request, response, key):
    ip = request.client.host if request.client else 'unknown'
    rate_limit('signup-ip:' + ip, 10)
    rate_limit('signup-email:' + data.email, 5)
    # Stable server-derived tenant scope permits retries without giving anonymous
    # callers platform access. The payload hash and current password both gate replay.
    org_id = uuid5(NAMESPACE_URL, 'dispatra:trial:' + key)
    context(db, org_id)
    actor = SimpleNamespace(id='trial:' + digest(key), organization_id=org_id)
    def create():
        company = OrganizationCreate(slug=data.slug, name=data.name, phone=data.phone, address=data.address,
            admin_login=data.email, admin_display_name=data.full_name, password=data.password)
        org, account = provision_company(db, company, organization_id=org_id, pending_verification=True)
        return {'user_id': str(account.id)}
    payload = data.model_dump(mode='json')
    # Blank addresses retain the payload shape used by older signup retries.
    if not data.address: payload.pop('address')
    result = once(db, actor, key, 'signup', payload, create)
    user = db.get(User, UUID(result['user_id']))
    if user: db.refresh(user, with_for_update=True)
    org = db.get(Organization, org_id)
    if not user or not user.active or not verify(data.password, user.password_hash):
        raise HTTPException(401, 'Please sign in with your current password.')
    if org and org.verification_pending:
        if not org._active: raise HTTPException(403, 'This company workspace is suspended. Contact Dispatra support.')
        from .verification import pending
        return pending(db, user, response)
    if not org or not org.active:
        raise HTTPException(403, 'This company workspace is suspended. Contact Dispatra support.')
    request.state.organization_id = org.id
    old = request.cookies.get(auth.COOKIE)
    if old: db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(old)))
    auth.start_session(db, user, response)
    user.last_login_at = now()
    audit(db, user, 'session.started', user.id, org.id)
    return auth.account_view(db, user)
