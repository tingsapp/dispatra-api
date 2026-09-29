import os
import secrets
from datetime import timedelta
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select, delete
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
    return AccountView(id=user.id, login_id=user.login_id, display_name=user.display_name, role=user.role, driver_id=user.driver_id, shipper_id=user.shipper_id,
                       organization=OrganizationView.model_validate(org) if org else None)

def start_session(db, user, response):
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=digest(token), user_id=user.id, organization_id=user.organization_id,
                        expires_at=now() + timedelta(hours=12)))
    response.set_cookie(COOKIE, token, httponly=True, secure=SECURE, samesite='strict', max_age=43200, path='/')

def login(db, data, request, response):
    ip = request.client.host if request.client else 'unknown'
    rate_limit('ip:' + ip, 100)
    rate_limit(f'login:{data.organization}:{data.login_id}', 10)
    # Suspended companies are resolved so a correct password can receive an explicit suspension notice.
    organization = db.scalar(select(Organization).where(Organization.slug == data.organization)) if data.organization else None
    scope = str(organization.id) if organization else 'platform'
    context(db, organization.id if organization else None)
    user = db.scalar(select(User).where(User.scope == scope, User.login_id == data.login_id).with_for_update())
    matches = verify(data.password, user.password_hash if user else DUMMY_HASH)
    role = {'platform':'ADMIN','dispatch':'DISPATCHER','customer':'SHIPPER','driver':'DRIVER'}[data.portal]
    valid_scope = (data.portal == 'platform' and data.organization is None) or (data.portal != 'platform' and organization is not None)
    if not user or not matches or not user.active or user.role != role or not valid_scope:
        raise HTTPException(401, 'Login ID or password is incorrect.')
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
    context(db, user.organization_id, user.role == 'ADMIN', user.shipper_id, user.driver_id)
    old = request.cookies.get(COOKIE)
    if old: db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(old)))
    start_session(db, user, response)
    user.last_login_at = now()
    audit(db, user, 'session.started', user.id, user.organization_id)
    return account_view(db, user)

def authenticated(request: Request, db: Session = Depends(session)):
    token = request.cookies.get(COOKIE, '')
    login_session = db.get(LoginSession, digest(token)) if token else None
    if not login_session or login_session.expires_at <= now(): raise HTTPException(401, 'Please sign in.')
    context(db, login_session.organization_id)
    user = db.get(User, login_session.user_id)
    if not user or not user.active: raise HTTPException(401, 'Please sign in.')
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

def change_password(db, user, data, response):
    # Serialize password/reset changes, including existing concurrent sessions.
    db.refresh(user, with_for_update=True)
    rate_limit('password:' + str(user.id), 10)
    if not verify(data.current_password, user.password_hash): raise HTTPException(400, 'Current password is incorrect.')
    user.password_hash = hash_password(data.new_password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    start_session(db, user, response)
    audit(db, user, 'password.changed', user.id, user.organization_id)


def driver(user: User = Depends(company_user)):
    if user.role != 'DRIVER': raise HTTPException(403, 'Driver access required.')
    return user
