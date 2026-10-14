import hashlib
import os
import re
import secrets
from types import SimpleNamespace
from datetime import timedelta
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select, delete, text
from sqlalchemy.orm import Session
from .database import session, context
from .models import User, Organization, LoginSession, now
from .security import digest, verify, hash_password, DUMMY_HASH, rate_limit
from .schemas import AccountView, OrganizationView
from .services import audit

COOKIE = 'dispatra_session'
SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() != 'false'

def account_view(db, user):
    org = db.get(Organization, user.organization_id) if user.organization_id else None
    tms_workspace = False
    if org and user.role == 'DISPATCHER':
        from .operations.models import Settings
        settings = db.scalar(select(Settings).where(Settings.organization_id == org.id))
        tms_workspace = bool(settings and settings.data.get('tms_workspace') is True)
    return AccountView(id=user.id, login_id=user.login_id, display_name=user.display_name, role=user.role, driver_id=user.driver_id, shipper_id=user.shipper_id,
                       organization=OrganizationView.model_validate(org) if org else None, tms_workspace=tms_workspace)

def start_session(db, user, response, mobile=False):
    token = ('dm_' if mobile else 'ws_') + secrets.token_urlsafe(32)
    started_at = now()
    expires_at = started_at + timedelta(hours=12)
    organization = db.get(Organization, user.organization_id) if user.organization_id else None
    if organization and organization.trial_ends_at and organization.trial_activated_at is None:
        expires_at = min(expires_at, organization.trial_ends_at)
    db.add(LoginSession(token_hash=digest(token), user_id=user.id, organization_id=user.organization_id,
                        expires_at=expires_at))
    if not mobile:
        response.set_cookie(COOKIE, token, httponly=True, secure=SECURE, samesite='strict', max_age=max(0, int((expires_at - started_at).total_seconds())), path='/')
    return {'token': token, 'expires_at': expires_at, 'account': account_view(db, user)}

def login(db, data, request, response, mobile=False):
    ip = request.client.host if request.client else 'unknown'
    rate_limit('ip:' + ip, 100)
    role = {'platform':'ADMIN','dispatch':'DISPATCHER','customer':'SHIPPER','driver':'DRIVER'}[data.portal]
    rate_limit(f'login:{role}:{data.login_id}', 10)
    # The SELECT-only policy exposes one role/login match before its tenant is known.
    # Clear that lookup context before locking or accessing any company records.
    db.execute(text("SELECT set_config('app.login_role', :role, true), set_config('app.login_id', :login, true)"),
               {'role': role, 'login': data.login_id})
    try:
        identity = db.execute(select(User.id, User.organization_id).where(
            User.role == role, User.login_id == data.login_id)).first()
    finally:
        db.execute(text("SELECT set_config('app.login_role', '', true), set_config('app.login_id', '', true)"))
    context(db, identity.organization_id if identity else None)
    user = db.scalar(select(User).where(User.id == identity.id, User.role == role,
        User.login_id == data.login_id).with_for_update()) if identity else None
    matches = verify(data.password, user.password_hash if user else DUMMY_HASH)
    organization = db.get(Organization, user.organization_id) if user and user.organization_id else None
    # An optional old company hint must match; new clients omit it entirely.
    valid_scope = (data.portal == 'platform' and data.organization is None) or (
        data.portal != 'platform' and organization is not None and
        (data.organization is None or data.organization == organization.slug))
    if not user or not matches or not user.active or not valid_scope:
        raise HTTPException(401, 'Email or password is incorrect.')
    if organization and organization.verification_pending and user.role == 'DISPATCHER' and not mobile:
        from .verification import pending
        return pending(db, user, response)
    if organization and not organization.active:
        raise HTTPException(403, 'This company workspace is suspended. Contact Dispatra support.')
    if user.driver_id:
        from .operations.models import Driver
        driver_record = db.get(Driver, user.driver_id)
        if not driver_record or not driver_record.active: raise HTTPException(401, 'Please sign in.')
    if user.shipper_id:
        from .operations.models import Shipper
        shipper = db.get(Shipper, user.shipper_id)
        if not shipper or shipper.status == 'INACTIVE': raise HTTPException(401, 'Login ID or password is incorrect.')
    request.state.organization_id = user.organization_id
    context(db, user.organization_id, user.role == 'ADMIN', user.shipper_id, user.driver_id)
    old = request.cookies.get(COOKIE)
    if old: db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(old)))
    issued = start_session(db, user, response, mobile)
    user.last_login_at = now()
    audit(db, user, 'session.started', user.id, user.organization_id)
    return issued if mobile else account_view(db, user)

# Paths an API key may call: the documented Orders, Shippers, Drivers, Vehicles and read-only catalogue endpoints.
API_KEY_PATHS = re.compile(r'^/api/v1/companies/[a-z0-9-]{1,63}/(orders(/[0-9a-f-]{36}(/(cancel|tracking|delivery-proof))?)?'
    r'|(shippers|drivers|vehicles)(/[0-9a-f-]{36}(/archive)?)?|catalog)$')

def request_token(request):
    authorization = request.headers.get('authorization')
    if authorization:
        if request.cookies.get(COOKIE) or not authorization.startswith(('Bearer dm_', 'Bearer dsp_')):
            raise HTTPException(401, 'Please sign in.')
        return authorization[7:]
    return request.cookies.get(COOKIE, '')

def api_key_principal(request, db, token):
    """A company API key acts with dispatcher permissions on the documented paths only; it is audited as `SYSTEM`."""
    from .integrations.models import ApiKey
    if not API_KEY_PATHS.match(request.url.path) or (request.url.path.endswith('/catalog') and request.method != 'GET'):
        raise HTTPException(403, 'API keys cannot use this endpoint.')
    context(db, platform=True)
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hashlib.sha256(token.encode()).hexdigest(), ApiKey.revoked_at.is_(None)))
    org = db.get(Organization, key.organization_id) if key else None
    if not key or not org or not org.active: raise HTTPException(401, 'Invalid API key.')
    request.state.organization_id = org.id
    context(db, org.id)
    if key.last_used_at is None or now() - key.last_used_at > timedelta(minutes=5): key.last_used_at = now()
    return SimpleNamespace(id=None, role='DISPATCHER', organization_id=org.id, shipper_id=None, driver_id=None, active=True,
        actor_type='SYSTEM', login_id='api-key', display_name='API key')

def authenticated(request: Request, db: Session = Depends(session)):
    token = request_token(request)
    if token.startswith('dsp_'): return api_key_principal(request, db, token)
    login_session = db.get(LoginSession, digest(token)) if token else None
    if not login_session or login_session.expires_at <= now(): raise HTTPException(401, 'Please sign in.')
    context(db, login_session.organization_id)
    user = db.get(User, login_session.user_id)
    if not user or not user.active: raise HTTPException(401, 'Please sign in.')
    if token.startswith('dm_') and user.role != 'DRIVER': raise HTTPException(403, 'Driver access required.')
    if user.organization_id:
        org = db.get(Organization, user.organization_id)
        if not org or not org.active: raise HTTPException(401, 'Please sign in.')
    if user.driver_id:
        from .operations.models import Driver
        driver_record = db.get(Driver, user.driver_id)
        if not driver_record or not driver_record.active: raise HTTPException(401, 'Please sign in.')
    if user.shipper_id:
        from .operations.models import Shipper
        shipper = db.get(Shipper, user.shipper_id)
        if not shipper or shipper.status == 'INACTIVE': raise HTTPException(401, 'Please sign in.')
    request.state.organization_id = user.organization_id
    context(db, user.organization_id, user.role == 'ADMIN', user.shipper_id, user.driver_id)
    return user

def platform(user: User = Depends(authenticated)):
    if user.role != 'ADMIN': raise HTTPException(403, 'Platform owner access required.')
    return user

def company_user(slug: str, user: User = Depends(authenticated), db: Session = Depends(session)):
    org = db.get(Organization, user.organization_id) if user.organization_id else None
    if not org or org.slug != slug: raise HTTPException(404, 'Workspace not found.')
    return user

def dispatcher(user: User = Depends(company_user)):
    if user.role != 'DISPATCHER': raise HTTPException(403, 'Dispatcher access required.')
    return user

def customer(user: User = Depends(company_user)):
    if user.role != 'SHIPPER': raise HTTPException(403, 'Shipper access required.')
    return user

def change_password(db, user, data, response, mobile=False):
    # Serialize password/reset changes, including existing concurrent sessions.
    db.refresh(user, with_for_update=True)
    rate_limit('password:' + str(user.id), 10)
    if not verify(data.current_password, user.password_hash): raise HTTPException(400, 'Current password is incorrect.')
    user.password_hash = hash_password(data.new_password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    issued = start_session(db, user, response, mobile)
    audit(db, user, 'password.changed', user.id, user.organization_id)
    return issued


def driver(user: User = Depends(company_user)):
    if user.role != 'DRIVER': raise HTTPException(403, 'Driver access required.')
    return user
