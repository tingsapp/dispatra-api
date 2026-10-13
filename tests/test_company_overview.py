"""Atomic company identity, dispatcher and workspace-access updates on PostgreSQL."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from conftest import HEADERS, PASSWORD, company, customer, login, owner_engine

ROOT = '/api/v1/platform/organizations'


@pytest.fixture
def overview(client):
    login(client)
    org = company(client)
    org = client.get(f"{ROOT}/{org['id']}").json()
    account, = client.get(f"{ROOT}/{org['id']}/dispatchers").json()
    body = {'version': org['version'], 'name': 'Saved company', 'active': False,
            'dispatcher': {'id': account['id'], 'version': account['version'], 'login_id': 'main-desk', 'display_name': 'Saved dispatcher'}}
    return org, account, body


def save(client, org, body, key=None):
    return client.patch(f"{ROOT}/{org['id']}/overview", json=body, headers={'Idempotency-Key': key or str(uuid4())})


def assert_unchanged(client, org, account):
    current = client.get(f"{ROOT}/{org['id']}").json()
    dispatcher, = client.get(f"{ROOT}/{org['id']}/dispatchers").json()
    assert (current['name'], current['active'], current['version']) == (org['name'], True, 1)
    assert (dispatcher['login_id'], dispatcher['display_name'], dispatcher['version']) == (account['login_id'], account['display_name'], 1)
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT data->>'company_name' FROM company_settings WHERE organization_id = :id"), {'id': org['id']}).scalar() == org['name']
        assert db.execute(text("SELECT count(*) FROM audit_events WHERE organization_id = :id AND action IN ('organization.updated', 'organization.suspended', 'dispatcher.updated')"), {'id': org['id']}).scalar() == 0


def test_one_save_updates_all_fields_and_suspends_access_with_idempotent_replay(client, overview):
    org, account, body = overview
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        key = str(uuid4())
        response = save(client, org, body, key)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['company']['name'] == 'Saved company' and result['company']['active'] is False and result['company']['version'] == 2
        assert result['dispatcher']['login_id'] == 'main-desk' and result['dispatcher']['display_name'] == 'Saved dispatcher'
        assert result['dispatcher']['version'] == 2 and result['dispatcher']['active_session_count'] == 0
        assert save(client, org, body, key).json() == result
        assert desk.get('/api/v1/auth/me').status_code == 401
        assert desk.post('/api/v1/auth/login', json={'portal': 'dispatch', 'organization': 'acme', 'login_id': 'main-desk', 'password': PASSWORD}).status_code == 403
        body.update(version=2, active=True)
        body['dispatcher']['version'] = 2
        restored = save(client, org, body)
        assert restored.status_code == 200 and restored.json()['dispatcher']['version'] == 2
        assert desk.get('/api/v1/auth/me').status_code == 401
        login(desk, 'dispatch', 'acme', 'main-desk')
        assert desk.get('/api/v1/companies/acme/settings').json()['data']['company_name'] == 'Saved company'
        assert len(client.get(f"{ROOT}/{org['id']}/dispatchers").json()) == 1
    actions = {row['action'] for row in client.get(f"{ROOT}/{org['id']}/audit").json()}
    assert {'dispatcher.updated', 'organization.updated', 'organization.suspended', 'organization.activated'} <= actions


@pytest.mark.parametrize('record', ['company', 'dispatcher'])
def test_stale_versions_do_not_save_any_part(client, overview, record):
    org, account, body = overview
    if record == 'company': body['version'] = 99
    else: body['dispatcher']['version'] = 99
    assert save(client, org, body).status_code == 409
    assert_unchanged(client, org, account)


def test_login_conflicts_do_not_save_company_fields(client, overview):
    org, account, body = overview
    company(client, 'other')
    body['dispatcher']['login_id'] = 'other-dispatcher'
    assert save(client, org, body).status_code == 409
    assert_unchanged(client, org, account)


def test_failure_after_identity_updates_rolls_back_every_write(client, overview, monkeypatch):
    org, account, body = overview
    def fail(*args): raise HTTPException(503, 'Access change failed.')
    monkeypatch.setattr('app.platform.service.apply_company_access', fail)
    assert save(client, org, body).status_code == 503
    assert_unchanged(client, org, account)


def test_foreign_dispatcher_and_non_owner_requests_are_rejected(client, overview):
    org, account, body = overview
    foreign = company(client, 'other')
    foreign_account, = client.get(f"{ROOT}/{foreign['id']}/dispatchers").json()
    body['dispatcher']['id'] = foreign_account['id']
    assert save(client, org, body).status_code == 404
    assert_unchanged(client, org, account)
    body['dispatcher']['id'] = account['id']
    with TestClient(app, headers=HEADERS) as anonymous:
        assert save(anonymous, org, body).status_code == 401
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert save(client, org, body).status_code == 403


def test_invalid_fields_are_rejected_before_any_changes(client, overview):
    org, account, body = overview
    body['dispatcher']['login_id'] = 'Invalid Login'
    assert save(client, org, body).status_code == 422
    assert_unchanged(client, org, account)


def test_supplied_password_changes_atomically_and_never_enters_saved_results(client, overview):
    org, account, body = overview
    password = '  Supplied-dispatch-password-12!  '
    body['active'] = True
    body['dispatcher'].update(login_id='dispatch@example.com', new_password=password)
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        key = str(uuid4())
        response = save(client, org, body, key)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['dispatcher']['version'] == 2 and result['dispatcher']['active_session_count'] == 0
        assert password not in response.text and 'new_password' not in response.text
        assert desk.get('/api/v1/auth/me').status_code == 401
        assert desk.post('/api/v1/auth/login', json={'portal': 'dispatch', 'organization': 'acme', 'login_id': 'dispatch@example.com', 'password': PASSWORD}).status_code == 401
        login(desk, 'dispatch', 'acme', 'dispatch@example.com', password)
        assert save(client, org, body, key).json() == result
        assert desk.get('/api/v1/auth/me').status_code == 200, 'a replay does not revoke new sessions'
    with owner_engine.connect() as db:
        stored = str(db.execute(text('SELECT result FROM operations')).all()) + str(db.execute(text('SELECT * FROM audit_events')).all())
        assert password not in stored and 'new_password' not in stored
    actions = [row['action'] for row in client.get(f"{ROOT}/{org['id']}/audit").json()]
    assert 'dispatcher.password_changed' in actions


def test_password_is_not_changed_when_another_field_fails(client, overview, monkeypatch):
    org, account, body = overview
    body['dispatcher']['new_password'] = 'Unsaved-dispatch-password-12!'
    def fail(*args): raise HTTPException(503, 'Access change failed.')
    monkeypatch.setattr('app.platform.service.apply_company_access', fail)
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        assert save(client, org, body).status_code == 503
        assert desk.get('/api/v1/auth/me').status_code == 200
        login(desk, 'dispatch', 'acme', 'dispatcher')
    assert_unchanged(client, org, account)


def test_short_password_is_rejected_before_saving(client, overview):
    org, account, body = overview
    body['dispatcher']['new_password'] = 'short'
    response = save(client, org, body)
    assert response.status_code == 422 and 'short' not in response.text
    assert_unchanged(client, org, account)
