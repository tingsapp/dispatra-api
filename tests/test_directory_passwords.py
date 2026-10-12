"""Dispatcher-supplied Shipper/Driver passwords, boundaries and replay on restricted PostgreSQL."""
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.main import app
from app.models import User, Operation, AuditEvent, now
from app.operations.models import DutySession
from app.security import verify
from conftest import HEADERS, login, company, post, owner_engine
from test_manual_operations import setup, BASE, check


@pytest.mark.parametrize('kind', ['shipper', 'driver'])
def test_dispatcher_supplied_password_revokes_target_sessions_and_replays_safely(setup, kind):
    w = setup
    row = w[kind]
    path = BASE + f'/{kind}s/{row["id"]}/password'
    password = '  Dispatcher-supplied-password-2026!  '
    key = str(uuid4())
    body = {'version': row['version'], 'new_password': password}
    portal = 'customer' if kind == 'shipper' else 'driver'
    with TestClient(app, headers=HEADERS) as target:
        login(target, portal, 'acme', row['email'], row['initial_password'])
        result = check(post(w['client'], path, body, key))
        assert result == {'id': row['id'], 'version': row['version'] + 1}
        assert target.get('/api/v1/auth/me').status_code == 401
        assert w['client'].get('/api/v1/auth/me').status_code == 200
        assert target.post('/api/v1/auth/login', json={'portal': portal, 'organization': 'acme', 'login_id': row['email'], 'password': row['initial_password']}).status_code == 401
        login(target, portal, 'acme', row['email'], password)
        if kind == 'driver':
            assert w['mobile'].get('/api/v1/auth/me').status_code == 401
            with Session(owner_engine) as db:
                assert db.get(DutySession, UUID(w['duty']['id'])).ended_at is not None
        # A replay must not reset a later self-service password or revoke its new session.
        later = 'Self-service-password-after-reset!'
        assert target.post('/api/v1/auth/password', json={'current_password': password, 'new_password': later}).status_code == 204
        assert check(post(w['client'], path, body, key)) == result
        assert target.get('/api/v1/auth/me').status_code == 200
        assert post(w['client'], path, {**body, 'new_password': later}, key).status_code == 409
        with Session(owner_engine) as db:
            column = User.shipper_id if kind == 'shipper' else User.driver_id
            account = db.scalar(select(User).where(column == UUID(row['id'])))
            assert verify(later, account.password_hash)
            saved = str([(item.payload_hash, item.result) for item in db.scalars(select(Operation))])
            assert password not in saved and 'new_password' not in saved
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == f'{kind}.password_reset')) == 1


@pytest.mark.parametrize('kind', ['shipper', 'driver'])
def test_stale_and_invalid_passwords_preserve_credentials_and_sessions(setup, kind):
    w = setup
    row = w[kind]
    path = BASE + f'/{kind}s/{row["id"]}/password'
    assert post(w['client'], path, {'version': row['version'] + 1, 'new_password': 'Stale-password-2026!'}).status_code == 409
    for password in ('short', 'x' * 129):
        response = post(w['client'], path, {'version': row['version'], 'new_password': password})
        assert response.status_code == 422 and password not in response.text
    with TestClient(app, headers=HEADERS) as target:
        login(target, 'customer' if kind == 'shipper' else 'driver', 'acme', row['email'], row['initial_password'])
    assert w['mobile'].get('/api/v1/auth/me').status_code == 200


@pytest.mark.parametrize('kind', ['shipper', 'driver'])
def test_password_changes_require_same_company_dispatcher_and_unarchived_account(setup, kind):
    w = setup
    row = w[kind]
    path = BASE + f'/{kind}s/{row["id"]}/password'
    body = {'version': row['version'], 'new_password': 'Only-own-company-password!'}
    with TestClient(app, headers=HEADERS) as denied:
        assert post(denied, path, body).status_code == 401
        login(denied, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        assert post(denied, path, body).status_code == 403
        login(denied, 'driver', 'acme', w['driver']['email'], w['driver']['initial_password'])
        assert post(denied, path, body).status_code == 403
        login(denied)
        assert post(denied, path, body).status_code == 404
        company(denied, 'other')
        login(denied, 'dispatch', 'other', 'dispatcher')
        assert post(denied, '/api/v1/companies/other' + f'/{kind}s/{row["id"]}/password', body).status_code == 404
    if kind == 'driver':
        assert check(post(w['mobile'], BASE + f'/driver/duty/{w["duty"]["id"]}/end', {'version': w['duty']['version'], 'ended_at': now().isoformat()}))
    archived = post(w['client'], BASE + f'/{kind}s/{row["id"]}/archive', {'version': row['version']})
    assert archived.status_code == 200, archived.text
    assert post(w['client'], path, {**body, 'version': archived.json()['version']}).status_code == 409
