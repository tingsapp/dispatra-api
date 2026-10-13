"""Restricted verification cookies cannot authenticate or grant a workspace session."""
import secrets
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, text, update
from fastapi import HTTPException
from app import auth
from app.database import context
from app.models import EmailVerification, Organization, User, OutboxEvent, now
from app.security import digest, hash_password, verify, rate_limit
from app.services import once, audit
from app.intake import vault
from app.operations import smtp_transport
from app.subscription import TRIAL_DAYS

COOKIE = 'dispatra_verification'

def configured():
    try: smtp_transport.configuration()
    except smtp_transport.PermanentFailure: raise HTTPException(503, 'Verification email is not configured. Please try again later.') from None
    vault.seal('configuration-check')

def view(row):
    return dict(verification_required=True, email=row.email, expires_at=row.expires_at,
                resend_at=row.resend_at, delivery_status=row.delivery_status)

def cookie(response, row):
    token = secrets.token_urlsafe(32)
    row.token_hash = digest(token)
    response.set_cookie(COOKIE, token, httponly=True, secure=auth.SECURE, samesite='strict', max_age=86400, path='/')
    response.delete_cookie(auth.COOKIE, path='/', secure=auth.SECURE, httponly=True, samesite='strict')

def create(db, user):
    configured()
    observed = now()
    db.execute(update(EmailVerification).where(EmailVerification.user_id == user.id,
        EmailVerification.consumed_at.is_(None)).values(consumed_at=observed, secret=None))
    code = f'{secrets.randbelow(1000000):06d}'
    row = EmailVerification(id=uuid4(), organization_id=user.organization_id, user_id=user.id,
        email=user.login_id, token_hash=digest(secrets.token_urlsafe(32)), code_hash=hash_password(code),
        credential_hash=digest(user.password_hash),
        secret=vault.seal(code), expires_at=observed + timedelta(minutes=10), resend_at=observed + timedelta(seconds=60))
    db.add(row); db.flush()
    db.add(OutboxEvent(organization_id=user.organization_id, event_type='verification.requested', entity_id=row.id))
    audit(db, user, 'account.verification_requested', row.id, user.organization_id)
    return row

def pending(db, user, response):
    row = db.scalar(select(EmailVerification).where(EmailVerification.user_id == user.id,
        EmailVerification.consumed_at.is_(None)).order_by(EmailVerification.expires_at.desc()).with_for_update())
    if not row or row.email != user.login_id or row.credential_hash != digest(user.password_hash) or row.expires_at <= now(): row = create(db, user)
    cookie(response, row)
    return view(row)

def current(db, request):
    token = request.cookies.get(COOKIE, '')
    if not token: raise HTTPException(401, 'Sign in to resume email verification.')
    db.execute(text("SELECT set_config('app.verification_token', :token, true)"), {'token':digest(token)})
    try: identity = db.execute(select(EmailVerification.id, EmailVerification.organization_id, EmailVerification.user_id).where(EmailVerification.token_hash == digest(token))).first()
    finally: db.execute(text("SELECT set_config('app.verification_token', '', true)"))
    if not identity: raise HTTPException(401, 'Sign in to resume email verification.')
    context(db, identity.organization_id)
    org = db.scalar(select(Organization).where(Organization.id == identity.organization_id).with_for_update(key_share=True))
    user = db.scalar(select(User).where(User.id == identity.user_id).with_for_update())
    row = db.scalar(select(EmailVerification).where(EmailVerification.id == identity.id).with_for_update())
    if row.token_hash != digest(token) or (row.consumed_at and org.verification_pending):
        raise HTTPException(401, 'Sign in to resume email verification.')
    if not user or not user.active or user.login_id != row.email or row.credential_hash != digest(user.password_hash) or not org or not org._active:
        raise HTTPException(403, 'Verification is unavailable. Please sign in or contact Dispatra.')
    request.state.organization_id = org.id
    return row, user, org

def resend(db, request, response, key):
    rate_limit('verification-resend-ip:' + request.client.host, 20)
    row, user, org = current(db, request)
    if not org.verification_pending: raise HTTPException(409, 'Email is already verified. Please sign in.')
    def run():
        if row.resend_at > now(): raise HTTPException(429, 'Wait a minute before requesting another code.')
        rate_limit('verification-resend-user:' + str(user.id), 5)
        return {'id':str(create(db, user).id)}
    result = once(db, user, key, 'verification-resend', {}, run)
    fresh = db.get(EmailVerification, result['id'])
    cookie(response, fresh)
    return view(fresh)

def confirm(db, data, request, response, key):
    rate_limit('verification-ip:' + request.client.host, 50)
    row, user, org = current(db, request)
    if row.expires_at <= now(): raise HTTPException(400, 'This code expired. Request a new code.')
    if row.attempts >= 5: raise HTTPException(429, 'Too many incorrect codes. Request a new code.')
    if not verify(data.code, row.code_hash):
        row.attempts += 1
        from app.main import error
        # Return an error response so the failed-attempt increment commits.
        return error(request, 400, 'The code is incorrect. Please try again.')
    def run():
        if row.consumed_at is not None or not org.verification_pending:
            raise HTTPException(409, 'This code has already been used. Please sign in.')
        row.consumed_at = now(); row.secret = None
        org.verification_pending = False
        org.trial_started_at = now(); org.trial_ends_at = org.trial_started_at + timedelta(days=TRIAL_DAYS)
        org.trial_activated_at = None
        org.version += 1
        audit(db, user, 'organization.trial_started', org.id, org.id)
        audit(db, user, 'account.email_verified', user.id, org.id)
        return {'user_id':str(user.id)}
    once(db, user, key, 'verification-confirm', {'verification_id':str(row.id), 'code':data.code}, run)
    db.refresh(org)
    auth.start_session(db, user, response)
    response.delete_cookie(COOKIE, path='/', secure=auth.SECURE, httponly=True, samesite='strict')
    user.last_login_at = now()
    audit(db, user, 'session.started', user.id, org.id)
    return auth.account_view(db, user)
