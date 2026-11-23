"""Forgotten-password reset by emailed code on restricted PostgreSQL."""
from uuid import UUID
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.main import app
from app.intake import vault
from app.operations.models import DutySession
from conftest import HEADERS, login, post, owner_engine
from test_manual_operations import setup, BASE

pytestmark = pytest.mark.usefixtures('verification_mail')
REQUEST = '/api/v1/auth/password/reset/request'
CONFIRM = '/api/v1/auth/password/reset/confirm'
RESEND = '/api/v1/auth/password/reset/resend'
NEW_PASSWORD = 'Recovered-password-2026!'


def request(client, portal, login_id):
    response = post(client, REQUEST, {'portal': portal, 'login_id': login_id})
    assert response.status_code == 200, response.text
    return response.json()


def code(login_id):
    with owner_engine.connect() as db:
        secret = db.scalar(text("SELECT secret FROM email_verifications WHERE email=:email AND purpose='PASSWORD_RESET' AND consumed_at IS NULL"), {'email': login_id})
    return vault.unseal(secret)


def resets():
    with owner_engine.connect() as db:
        return db.scalar(text("SELECT count(*) FROM email_verifications WHERE purpose='PASSWORD_RESET'"))


def test_driver_reset_changes_password_revokes_sessions_and_ends_duty(setup):
    w = setup
    email = 'dana@example.com'
    started = request(w['client'], 'driver', email)
    assert started['email'] == email and started['reset_token']
    wrong = '999999' if code(email) != '999999' else '888888'
    assert post(w['client'], CONFIRM, {'reset_token': started['reset_token'], 'code': wrong, 'new_password': NEW_PASSWORD}).status_code == 400
    response = post(w['client'], CONFIRM, {'reset_token': started['reset_token'], 'code': code(email), 'new_password': NEW_PASSWORD})
    assert response.status_code == 200, response.text
    assert w['mobile'].get('/api/v1/auth/me').status_code == 401
    with Session(owner_engine) as db:
        assert db.get(DutySession, UUID(w['duty']['id'])).ended_at is not None
        row = db.execute(text("SELECT consumed_at, secret, attempts FROM email_verifications WHERE purpose='PASSWORD_RESET'")).one()
        assert row.consumed_at is not None and row.secret is None and row.attempts == 1
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='account.password_reset'")) == 1
    with TestClient(app, headers=HEADERS) as other:
        assert other.post('/api/v1/auth/login', json={'portal': 'driver', 'login_id': email, 'password': w['driver']['initial_password']}).status_code == 401
        login(other, 'driver', 'acme', email, NEW_PASSWORD)
    # The consumed request cannot be replayed.
    assert post(w['client'], CONFIRM, {'reset_token': started['reset_token'], 'code': '123456', 'new_password': NEW_PASSWORD}).status_code == 401


def test_unknown_wrong_role_and_inactive_accounts_look_identical(setup):
    w = setup
    known = request(w['client'], 'driver', 'dana@example.com')
    for portal, login_id in [('driver', 'nobody@example.com'), ('customer', 'dana@example.com'), ('dispatch', 'dispatcher')]:
        decoy = request(w['client'], portal, login_id)
        assert decoy.keys() == known.keys() and decoy['reset_token'] and decoy['delivery_status'] == 'PENDING'
        assert post(w['client'], CONFIRM, {'reset_token': decoy['reset_token'], 'code': '123456', 'new_password': NEW_PASSWORD}).status_code == 401
    assert resets() == 1


def test_failed_attempts_persist_and_lock_out(setup):
    w = setup
    email = 'alice@example.com'
    token = request(w['client'], 'customer', email)['reset_token']
    right = code(email)
    wrong = '999999' if right != '999999' else '888888'
    for _ in range(5):
        assert post(w['client'], CONFIRM, {'reset_token': token, 'code': wrong, 'new_password': NEW_PASSWORD}).status_code == 400
    assert post(w['client'], CONFIRM, {'reset_token': token, 'code': right, 'new_password': NEW_PASSWORD}).status_code == 429
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT attempts FROM email_verifications WHERE purpose='PASSWORD_RESET'")) == 5


def test_resend_replaces_the_request_and_tokens_stay_out_of_verification(setup):
    w = setup
    email = 'alice@example.com'
    first = request(w['client'], 'customer', email)
    assert post(w['client'], RESEND, {'reset_token': first['reset_token']}).status_code == 429
    with owner_engine.begin() as db:
        db.execute(text("UPDATE email_verifications SET resend_at=now()-interval '1 second'"))
    fresh = post(w['client'], RESEND, {'reset_token': first['reset_token']})
    assert fresh.status_code == 200, fresh.text
    second = fresh.json()['reset_token']
    assert post(w['client'], CONFIRM, {'reset_token': first['reset_token'], 'code': code(email), 'new_password': NEW_PASSWORD}).status_code == 401
    with TestClient(app, headers=HEADERS) as stranger:
        stranger.cookies.set('dispatra_verification', second)
        assert post(stranger, '/api/v1/auth/verification/confirm', {'code': code(email)}).status_code == 401
    assert post(w['client'], CONFIRM, {'reset_token': second, 'code': code(email), 'new_password': NEW_PASSWORD}).status_code == 200
