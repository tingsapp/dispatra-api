"""Forgotten-password recovery by emailed code. Responses never reveal whether an account exists."""
import secrets
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update

from app import auth
from app.database import context
from app.intake import vault
from app.models import EmailVerification, LoginSession, OutboxEvent, User, now
from app.operations import smtp_transport
from app.security import digest, hash_password, verify, rate_limit
from app.services import audit


COOKIE = 'dispatra_password_reset'
ROLES = {'platform': 'ADMIN', 'dispatch': 'DISPATCHER', 'customer': 'SHIPPER', 'driver': 'DRIVER'}


def configured():
    try:
        smtp_transport.configuration()
    except smtp_transport.PermanentFailure:
        raise HTTPException(503, 'Password reset email is not configured. Please try again later.') from None
    vault.seal('configuration-check')


def create(db, user):
    observed = now()
    db.execute(update(EmailVerification).where(
        EmailVerification.user_id == user.id,
        EmailVerification.consumed_at.is_(None),
        EmailVerification.purpose == 'PASSWORD_RESET',
    ).values(consumed_at=observed, secret=None))
    code = f'{secrets.randbelow(1000000):06d}'
    row = EmailVerification(
        id=uuid4(),
        organization_id=user.organization_id,
        user_id=user.id,
        email=user.login_id,
        token_hash=digest(secrets.token_urlsafe(32)),
        code_hash=hash_password(code),
        credential_hash=digest(user.password_hash),
        secret=vault.seal(code),
        expires_at=observed + timedelta(minutes=10),
        resend_at=observed + timedelta(seconds=60),
        purpose='PASSWORD_RESET',
    )
    db.add(row)
    db.flush()
    db.add(OutboxEvent(
        organization_id=user.organization_id,
        event_type='verification.requested',
        entity_id=row.id,
    ))
    audit(db, user, 'account.password_reset_requested', row.id, user.organization_id)
    return row


def issue(response, row):
    token = secrets.token_urlsafe(32)
    row.token_hash = digest(token)
    response.set_cookie(COOKIE, token, httponly=True,
                        secure=auth.SECURE, samesite='strict',
                        max_age=86400, path='/')
    return view(row, token)


def decoy(response, login_id):
    # Same shape and cookie as a real request; the token matches no row.
    observed = now()
    token = secrets.token_urlsafe(32)
    response.set_cookie(COOKIE, token, httponly=True,
                        secure=auth.SECURE, samesite='strict',
                        max_age=86400, path='/')
    return {'reset_required': True, 'email': login_id,
            'expires_at': observed + timedelta(minutes=10),
            'resend_at': observed + timedelta(seconds=60),
            'delivery_status': 'PENDING', 'reset_token': token}


def request_reset(db, request, response, data):
    configured()
    rate_limit('password-reset-request-ip:' + (request.client.host if request.client else 'unknown'), 20)
    role = ROLES[data.portal]
    login_id = data.login_id.strip().lower()
    rate_limit(f'password-reset-request:{role}:{login_id}', 5)
    if '@' not in login_id:
        return decoy(response, login_id)
    # The SELECT-only policy exposes one role/login match before its tenant is known.
    db.execute(text(
        "SELECT set_config('app.login_role', :role, true), set_config('app.login_id', :login, true)"
    ), {'role': role, 'login': login_id})
    try:
        identity = db.execute(select(User.id, User.organization_id).where(
            User.role == role, User.login_id == login_id)).first()
    finally:
        db.execute(text("SELECT set_config('app.login_role', '', true), set_config('app.login_id', '', true)"))
    if not identity:
        return decoy(response, login_id)
    context(db, identity.organization_id)
    user = db.scalar(select(User).where(User.id == identity.id).with_for_update())
    # Platform admins have no company to hold a reset request.
    if not user or not user.active or user.organization_id is None:
        return decoy(response, login_id)
    return issue(response, create(db, user))


def view(row, reset_token=None):
    return {
        'reset_required': True,
        'email': row.email,
        'expires_at': row.expires_at,
        'resend_at': row.resend_at,
        'delivery_status': row.delivery_status,
        'reset_token': reset_token,
    }


def current(db, request, supplied_token=None):
    token = supplied_token or request.cookies.get(COOKIE, '')
    if not token:
        raise HTTPException(401, 'Start password reset from the sign-in screen.')
    # Reuses the verification token lookup policy on email_verifications.
    db.execute(text("SELECT set_config('app.verification_token', :token, true)"),
               {'token': digest(token)})
    try:
        identity = db.execute(select(
            EmailVerification.id,
            EmailVerification.organization_id,
            EmailVerification.user_id,
        ).where(
            EmailVerification.token_hash == digest(token),
            EmailVerification.purpose == 'PASSWORD_RESET',
            EmailVerification.consumed_at.is_(None),
        ).limit(1)).first()
    finally:
        db.execute(text("SELECT set_config('app.verification_token', '', true)"))
    if not identity:
        raise HTTPException(401, 'This password reset request is no longer available.')
    context(db, identity.organization_id)
    row = db.scalar(select(EmailVerification).where(
        EmailVerification.id == identity.id,
        EmailVerification.purpose == 'PASSWORD_RESET',
    ).with_for_update())
    user = db.scalar(select(User).where(User.id == identity.user_id).with_for_update())
    if not row or not user or not user.active or row.token_hash != digest(token) or row.consumed_at is not None:
        raise HTTPException(401, 'This password reset request is no longer available.')
    if row.email != user.login_id or row.credential_hash != digest(user.password_hash):
        raise HTTPException(401, 'The account changed. Start password reset again.')
    if row.expires_at <= now():
        raise HTTPException(400, 'This password reset code expired. Request a new code.')
    return row, user


def status(db, request):
    row, _ = current(db, request)
    return view(row)


def confirm(db, request, response, data):
    rate_limit('password-reset-confirm-ip:' + (request.client.host if request.client else 'unknown'), 50)
    row, user = current(db, request, data.reset_token)
    if row.attempts >= 5:
        raise HTTPException(429, 'Too many incorrect codes. Request a new code.')
    if not verify(data.code, row.code_hash):
        row.attempts += 1
        from app.main import error
        # Return an error response so the failed-attempt increment commits.
        return error(request, 400, 'The code is incorrect. Please try again.')

    user.password_hash = hash_password(data.new_password)
    user.version += 1
    user.updated_at = now()
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    row.consumed_at = now()
    row.secret = None
    audit(db, user, 'account.password_reset', user.id, user.organization_id)
    response.delete_cookie(COOKIE, path='/',
                           secure=auth.SECURE, httponly=True, samesite='strict')
    if user.driver_id:
        from app.operations.execution import close_duty_on_logout
        close_duty_on_logout(db, user)
    return {'reset': True}


def resend(db, request, response, supplied_token=None):
    rate_limit('password-reset-resend-ip:' + (request.client.host if request.client else 'unknown'), 20)
    row, user = current(db, request, supplied_token)
    if row.resend_at > now():
        raise HTTPException(429, 'Wait a minute before requesting another code.')
    rate_limit('password-reset-resend-user:' + str(user.id), 5)
    configured()
    return issue(response, create(db, user))
