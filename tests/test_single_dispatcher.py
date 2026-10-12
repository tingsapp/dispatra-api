"""One dispatcher per company, including concurrency and legacy account migration."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.main import app
from conftest import HEADERS, PASSWORD, company, login, owner_engine, post

ROOT = '/api/v1/platform/organizations'


def test_simultaneous_first_account_creation_has_one_winner(client):
    org_id = uuid4()
    with owner_engine.begin() as db:
        db.execute(text("""INSERT INTO organizations (id, slug, name, active, version, created_at, updated_at)
            VALUES (:id, 'empty', 'Empty company', true, 1, now(), now())"""), {'id': org_id})
    def create(login_id):
        with TestClient(app, headers=HEADERS) as owner:
            login(owner)
            return post(owner, f'{ROOT}/{org_id}/dispatchers', {'login_id': login_id})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ('desk-one', 'desk-two')))
    assert sorted(r.status_code for r in results) == [201, 409]
    created = next(r.json() for r in results if r.status_code == 201)
    assert created['initial_password']
    login(client)
    assert len(client.get(f'{ROOT}/{org_id}/dispatchers').json()) == 1


def test_inactive_account_still_occupies_the_company_slot(client):
    login(client)
    org = company(client)
    base = f"{ROOT}/{org['id']}/dispatchers"
    account, = client.get(base).json()
    with owner_engine.begin() as db:
        db.execute(text('UPDATE users SET active = false WHERE id = :id'), {'id': account['id']})
    assert post(client, base, {'login_id': 'replacement'}).status_code == 409
    assert post(client, base + f"/{account['id']}/activate", {'version': 1}).status_code == 200
    key = str(uuid4())
    response = post(client, base + f"/{account['id']}/reset-password", {'version': 2}, key)
    assert response.status_code == 200 and response.json()['initial_password']
    assert post(client, base + f"/{account['id']}/reset-password", {'version': 2}, key).json()['initial_password'] is None


def test_legacy_migration_keeps_oldest_active_revokes_extras_and_preserves_history(client, monkeypatch):
    login(client)
    org = company(client)
    other = company(client, 'other')
    base = f"{ROOT}/{org['id']}/dispatchers"
    first, = client.get(base).json()
    extra, inactive = uuid4(), uuid4()
    path = Path(__file__).parents[1] / 'migrations/versions/0036_single_dispatcher.py'
    spec = importlib.util.spec_from_file_location('single_dispatcher_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with owner_engine.begin() as db:
        monkeypatch.setattr(migration.op, 'execute', lambda sql: db.execute(text(sql)))
        migration.downgrade()
        for identity, login_id, active, interval in [(extra, 'extra', True, '1 second'), (inactive, 'historical', False, '-1 day')]:
            db.execute(text("""INSERT INTO users
                (id, organization_id, scope, login_id, role, password_hash, active, display_name, version, created_at, updated_at)
                SELECT :id, organization_id, scope, :login, role, password_hash, :active, :login, 1,
                    created_at + CAST(:interval AS interval), updated_at FROM users WHERE id = :first"""),
                {'id': identity, 'login': login_id, 'active': active, 'interval': interval, 'first': first['id']})
        for identity, token in [(first['id'], 'a' * 64), (extra, 'b' * 64)]:
            db.execute(text("""INSERT INTO login_sessions (token_hash, user_id, organization_id, expires_at)
                VALUES (:token, :user, :org, now() + interval '1 hour')"""), {'token': token, 'user': identity, 'org': org['id']})
        before_count = db.execute(text('SELECT count(*) FROM users')).scalar()
        migration.upgrade()
        assert db.execute(text('SELECT count(*) FROM users')).scalar() == before_count
        assert db.execute(text("SELECT count(*) FROM users WHERE organization_id = :org AND role = 'DISPATCHER' AND active"), {'org': org['id']}).scalar() == 1
        assert db.execute(text('SELECT active FROM users WHERE id = :id'), {'id': first['id']}).scalar() is True
        assert db.execute(text('SELECT active FROM users WHERE id = :id'), {'id': inactive}).scalar() is False
        assert db.execute(text('SELECT version FROM users WHERE id = :id'), {'id': extra}).scalar() == 2
        assert db.execute(text('SELECT count(*) FROM login_sessions WHERE user_id = :id'), {'id': extra}).scalar() == 0
        assert db.execute(text('SELECT count(*) FROM login_sessions WHERE user_id = :id'), {'id': first['id']}).scalar() == 1
        assert db.execute(text("SELECT count(*) FROM users WHERE organization_id = :org AND role = 'DISPATCHER' AND active"), {'org': other['id']}).scalar() == 1
        assert db.execute(text("SELECT count(*) FROM audit_events WHERE entity_id = :id AND action = 'dispatcher.deactivated'"), {'id': extra}).scalar() == 1
        migration.downgrade()
        assert db.execute(text('SELECT active FROM users WHERE id = :id'), {'id': extra}).scalar() is False
        migration.upgrade()
    blocked = post(client, base + f'/{extra}/activate', {'version': 2})
    assert blocked.status_code == 409 and 'Only one dispatcher account' in blocked.text
    with pytest.raises(IntegrityError):
        with owner_engine.begin() as db:
            db.execute(text('UPDATE users SET active = true WHERE id = :id'), {'id': extra})
    with TestClient(app, headers=HEADERS) as legacy:
        response = legacy.post('/api/v1/auth/login', json={'portal': 'dispatch', 'organization': 'acme', 'login_id': 'extra', 'password': PASSWORD})
        assert response.status_code == 401
