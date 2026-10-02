"""Platform owner administration on real PostgreSQL with the restricted runtime role."""
import os
import subprocess
import sys
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.main import app
from app.models import User
from app import bootstrap
from conftest import login, company, customer, post, PASSWORD, HEADERS, owner_engine

ROOT = '/api/v1/platform/organizations'


def check(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def ids(client):
    return {c['slug']: c['id'] for c in check(client.get(ROOT))}


def dispatcher(client, org_id, login_id='second', key=None):
    return check(post(client, f'{ROOT}/{org_id}/dispatchers', dict(login_id=login_id, display_name='Second Dispatcher'), key), 201)


def command(client, path, body=None, key=None):
    return client.post(path, json=body if body is not None else {}, headers={'Idempotency-Key': key or str(uuid4())})


def patch(client, path, body, key=None):
    return client.patch(path, json=body, headers={'Idempotency-Key': key or str(uuid4())})


@pytest.fixture
def companies(client):
    login(client); company(client); company(client, 'other')
    return ids(client)


def test_platform_endpoints_require_owner_and_owner_cannot_use_company_endpoints(client, companies):
    acme = companies['acme']
    reads = [ROOT, f'{ROOT}/{acme}', f'{ROOT}/{acme}/dispatchers', f'{ROOT}/{acme}/audit']
    with TestClient(app, headers=HEADERS) as anonymous:
        for path in reads: assert anonymous.get(path).status_code == 401
        assert command(anonymous, f'{ROOT}/{acme}/suspend', {'version': 1}).status_code == 401
    # The owner cannot silently act as a dispatcher through tenant endpoints.
    for path in ['/api/v1/companies/acme/settings', '/api/v1/companies/acme/customers', '/api/v1/companies/acme/orders', '/api/v1/companies/acme/drivers']:
        assert client.get(path).status_code == 404, path
    assert post(client, '/api/v1/companies/acme/customers', dict(name='X', number='X', login_id='x-login', password=PASSWORD)).status_code == 404
    login(client, 'dispatch', 'acme', 'dispatcher')
    for path in reads: assert client.get(path).status_code == 403
    for path in [f'{ROOT}/{acme}/suspend', f'{ROOT}/{acme}/dispatchers']:
        assert command(client, path, {'version': 1, 'login_id': 'intruder'}).status_code in (403, 422)
    assert command(client, f'{ROOT}/{acme}/suspend', {'version': 1}).status_code == 403
    customer(client)
    login(client, 'customer', 'acme', 'customer')
    for path in reads: assert client.get(path).status_code == 403


def test_company_search_detail_edit_and_audit(client, companies):
    acme = companies['acme']
    assert [c['slug'] for c in check(client.get(ROOT, params={'search': 'acm'}))] == ['acme']
    assert check(client.get(ROOT, params={'search': '%'})) == []
    assert [c['slug'] for c in check(client.get(ROOT, params={'limit': 1}))] == ['acme']
    assert [c['slug'] for c in check(client.get(ROOT, params={'limit': 1, 'after': acme}))] == ['other']
    detail = check(client.get(f'{ROOT}/{acme}'))
    assert detail['dispatcher_count'] == detail['active_dispatcher_count'] == 1 and detail['version'] == 1
    assert client.get(f'{ROOT}/{uuid4()}').status_code == 404
    key = str(uuid4())
    edited = check(patch(client, f'{ROOT}/{acme}', {'version': 1, 'name': 'Acme Logistics'}, key))
    assert edited['name'] == 'Acme Logistics' and edited['version'] == 2
    assert check(patch(client, f'{ROOT}/{acme}', {'version': 1, 'name': 'Acme Logistics'}, key)) == edited
    assert patch(client, f'{ROOT}/{acme}', {'version': 1, 'name': 'Stale'}).status_code == 409
    assert patch(client, f'{ROOT}/{acme}', {'version': 2, 'name': 'Acme', 'slug': 'renamed'}).status_code == 422
    assert check(client.get(ROOT, params={'status': 'ACTIVE'}))[0]['name'] == 'Acme Logistics'
    actions = [e['action'] for e in check(client.get(f'{ROOT}/{acme}/audit'))]
    assert actions[0] == 'organization.updated' and 'organization.created' in actions and 'dispatcher.created' in actions
    assert all(e['actor_role'] == 'ADMIN' for e in check(client.get(f'{ROOT}/{acme}/audit')))
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert check(client.get('/api/v1/companies/acme/settings'))['data']['company_name'] == 'Acme Logistics'
    assert check(client.get('/api/v1/auth/me'))['organization']['name'] == 'Acme Logistics'


def test_dispatcher_lifecycle_reset_revoke_and_last_active_guard(client, companies):
    acme = companies['acme']
    key = str(uuid4())
    created = dispatcher(client, acme, key=key)
    password = created['initial_password']
    assert len(password) >= 20 and created['active'] and created['last_login_at'] is None
    replay = dispatcher(client, acme, key=key)
    assert replay['id'] == created['id'] and replay['initial_password'] is None
    assert post(client, f'{ROOT}/{acme}/dispatchers', dict(login_id='second')).status_code == 409
    assert post(client, f'{ROOT}/{acme}/dispatchers', dict(login_id='dispatcher')).status_code == 409
    other_login = post(client, f"{ROOT}/{companies['other']}/dispatchers", dict(login_id='second'))
    assert other_login.status_code == 201, 'login IDs are unique per company, not globally'
    base = f"{ROOT}/{acme}/dispatchers/{created['id']}"
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'second', password)
        listed = next(d for d in check(client.get(f'{ROOT}/{acme}/dispatchers')) if d['id'] == created['id'])
        assert listed['last_login_at'] and listed['active_session_count'] == 1 and 'initial_password' not in listed
        updated = check(patch(client, base, {'version': 1, 'login_id': 'second-desk', 'display_name': 'Desk Two'}))
        assert updated['version'] == 2 and updated['login_id'] == 'second-desk'
        assert patch(client, base, {'version': 1, 'login_id': 'x-desk'}).status_code == 409
        assert patch(client, base, {'version': 2, 'login_id': 'dispatcher'}).status_code == 409
        reset = check(command(client, base + '/reset-password', {'version': 2}))
        assert reset['initial_password'] and reset['initial_password'] != password and reset['version'] == 3
        assert desk.get('/api/v1/auth/me').status_code == 401
        assert desk.post('/api/v1/auth/login', json=dict(portal='dispatch', organization='acme', login_id='second-desk', password=password)).status_code == 401
        login(desk, 'dispatch', 'acme', 'second-desk', reset['initial_password'])
        assert check(command(client, base + '/revoke-sessions')) == {'revoked': 1}
        assert desk.get('/api/v1/auth/me').status_code == 401
        login(desk, 'dispatch', 'acme', 'second-desk', reset['initial_password'])
        deactivated = check(command(client, base + '/deactivate', {'version': 3}))
        assert not deactivated['active'] and deactivated['active_session_count'] == 0
        assert desk.get('/api/v1/auth/me').status_code == 401
        assert desk.post('/api/v1/auth/login', json=dict(portal='dispatch', organization='acme', login_id='second-desk', password=reset['initial_password'])).status_code == 401
    first = next(d for d in check(client.get(f'{ROOT}/{acme}/dispatchers')) if d['login_id'] == 'dispatcher')
    blocked = command(client, f"{ROOT}/{acme}/dispatchers/{first['id']}/deactivate", {'version': first['version']})
    assert blocked.status_code == 409 and 'at least one active dispatcher' in blocked.text
    assert command(client, base + '/deactivate', {'version': 4}).status_code == 409
    check(command(client, base + '/activate', {'version': 4}))
    check(command(client, f"{ROOT}/{acme}/dispatchers/{first['id']}/deactivate", {'version': first['version']}))
    actions = {e['action'] for e in check(client.get(f'{ROOT}/{acme}/audit'))}
    assert {'dispatcher.created', 'dispatcher.updated', 'dispatcher.password_reset', 'dispatcher.sessions_revoked',
            'dispatcher.deactivated', 'dispatcher.activated'} <= actions
    with owner_engine.connect() as db:
        stored = str(db.execute(text('SELECT result FROM operations')).all()) + str(db.execute(text('SELECT * FROM audit_events')).all())
        assert password not in stored and reset['initial_password'] not in stored and 'initial_password' not in stored


def test_cross_company_dispatcher_isolation(client, companies):
    acme, other = companies['acme'], companies['other']
    created = dispatcher(client, acme)
    foreign = f"{ROOT}/{other}/dispatchers/{created['id']}"
    assert patch(client, foreign, {'version': 1, 'login_id': 'stolen'}).status_code == 404
    for action in ['reset-password', 'deactivate', 'activate']:
        assert command(client, f'{foreign}/{action}', {'version': 1}).status_code == 404
    assert command(client, f'{foreign}/revoke-sessions').status_code == 404
    assert created['id'] not in {d['id'] for d in check(client.get(f'{ROOT}/{other}/dispatchers'))}
    assert client.get(f'{ROOT}/{uuid4()}/dispatchers').status_code == 404
    assert post(client, f'{ROOT}/{uuid4()}/dispatchers', dict(login_id='ghost')).status_code == 404
    # A dispatcher of one company cannot sign in to another company's workspace.
    with TestClient(app, headers=HEADERS) as desk:
        assert desk.post('/api/v1/auth/login', json=dict(portal='dispatch', organization='other', login_id='second', password=created['initial_password'])).status_code == 401


def test_company_suspension_revokes_access_and_preserves_records(client, companies):
    acme = companies['acme']
    login(client, 'dispatch', 'acme', 'dispatcher')
    shipper = customer(client)
    with TestClient(app, headers=HEADERS) as owner, TestClient(app, headers=HEADERS) as portal:
        login(owner); login(portal, 'customer', 'acme', 'customer')
        with owner_engine.connect() as db: before = db.scalar(text('SELECT count(*) FROM shippers'))
        key = str(uuid4())
        suspended = check(command(owner, f'{ROOT}/{acme}/suspend', {'version': 1}, key))
        assert suspended['active'] is False and suspended['active_session_count'] == 0 and suspended['version'] == 2
        assert check(command(owner, f'{ROOT}/{acme}/suspend', {'version': 1}, key)) == suspended
        assert command(owner, f'{ROOT}/{acme}/suspend', {'version': 2}).status_code == 409
        for session in (client, portal): assert session.get('/api/v1/auth/me').status_code == 401
        denied = client.post('/api/v1/auth/login', json=dict(portal='dispatch', organization='acme', login_id='dispatcher', password=PASSWORD))
        assert denied.status_code == 403 and 'suspended' in denied.text
        wrong = client.post('/api/v1/auth/login', json=dict(portal='dispatch', organization='acme', login_id='dispatcher', password='Wrong-password-123!'))
        assert wrong.status_code == 401 and 'suspended' not in wrong.text
        assert command(owner, f"{ROOT}/{companies['other']}/suspend", {'version': 1}).status_code == 200
        check(command(owner, f"{ROOT}/{companies['other']}/activate", {'version': 2}))
        assert [c['slug'] for c in check(owner.get(ROOT, params={'status': 'SUSPENDED'}))] == ['acme']
        with owner_engine.connect() as db:
            assert db.scalar(text('SELECT count(*) FROM shippers')) == before
        check(command(owner, f'{ROOT}/{acme}/activate', {'version': 2}))
        assert portal.get('/api/v1/auth/me').status_code == 401, 'suspension sessions are not restored'
        login(portal, 'customer', 'acme', 'customer')
        assert check(portal.get('/api/v1/companies/acme/profile'))['id'] == shipper['id']
        actions = [e['action'] for e in check(owner.get(f'{ROOT}/{acme}/audit'))]
        assert actions[:2] == ['organization.activated', 'organization.suspended']


def test_suspension_closes_open_driver_duty(client, companies):
    base = '/api/v1/companies/acme'
    login(client, 'dispatch', 'acme', 'dispatcher')
    address = dict(text='123 Main Street, Vancouver, BC V5Y 1V4, Canada', city='Vancouver', province='BC', postal_code='V5Y 1V4', latitude=49.26, longitude=-123.11)
    driver = check(post(client, base + '/drivers', dict(name='Dana Driver', email='dana@example.com', phone='6045550102', address=address)), 201)
    with TestClient(app, headers=HEADERS) as mobile:
        login(mobile, 'driver', 'acme', 'dana@example.com', driver['initial_password'])
        check(post(mobile, base + '/driver/duty', {'location_permission': 'GRANTED'}), 201)
        login(client)
        check(command(client, f"{ROOT}/{companies['acme']}/suspend", {'version': 1}))
        assert mobile.get('/api/v1/auth/me').status_code == 401
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM duty_sessions WHERE ended_at IS NULL')) == 0


def test_bootstrap_single_owner_recovery_and_password_policy(client):
    assert bootstrap.password_problem('owner', 'short') and bootstrap.password_problem('owner', 'alllowercaseletters')
    assert bootstrap.password_problem('admin', 'Admin-Password-123') and bootstrap.password_problem('x@y.z', 'aaaaaaaaaaaA1!')
    assert bootstrap.password_problem('owner', 'Correct-Horse-9-Battery') is None
    assert bootstrap.password_problem('owner', 'correct horse battery staple') is None
    with Session(owner_engine) as db, db.begin():
        assert bootstrap.migration_problem(db) is None
    with pytest.raises(SystemExit):
        with Session(owner_engine) as db, db.begin(): bootstrap.create_owner(db, 'second-owner', 'Another-Owner-Pass-1')
    with pytest.raises(IntegrityError):
        with Session(owner_engine) as db, db.begin():
            db.add(User(scope='platform', login_id='second-owner', role='ADMIN', password_hash='not-a-hash')); db.flush()
    login(client)
    for _ in range(3): client.post('/api/v1/auth/login', json=dict(portal='platform', login_id='owner', password='wrong'))
    with Session(owner_engine) as db, db.begin(): bootstrap.reset_owner_password(db, 'Recovered-Owner-Pass-7')
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.post('/api/v1/auth/login', json=dict(portal='platform', login_id='owner', password=PASSWORD)).status_code == 401
    me = login(client, password='Recovered-Owner-Pass-7')
    assert me['role'] == 'ADMIN' and me['organization'] is None
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action = 'platform_owner.password_reset'")) == 1
        assert db.scalar(text("SELECT count(*) FROM users WHERE role = 'ADMIN'")) == 1


def test_bootstrap_create_on_empty_database():
    with owner_engine.begin() as db: db.execute(text('TRUNCATE users CASCADE'))
    with Session(owner_engine) as db, db.begin(): bootstrap.create_owner(db, 'founder', 'Founder-Owner-Pass-5')
    with TestClient(app, headers=HEADERS) as client:
        assert login(client, login_id='founder', password='Founder-Owner-Pass-5')['role'] == 'ADMIN'
    with owner_engine.connect() as db:
        stored = db.scalar(text("SELECT password_hash FROM users WHERE login_id = 'founder'"))
        assert stored.startswith('$argon2') and 'Founder-Owner-Pass-5' not in stored


def run_bootstrap(*args, script=None):
    env = {**os.environ, 'MIGRATION_DATABASE_URL': os.environ['TEST_OWNER_DATABASE_URL']}
    command = [sys.executable, '-c', script] if script else [sys.executable, '-m', 'app.bootstrap', *args]
    return subprocess.run(command, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                          cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_bootstrap_command_in_a_fresh_interpreter():
    status = run_bootstrap('--status')
    assert status.returncode == 0 and 'Platform owner exists: owner' in status.stdout, status.stderr
    rerun = run_bootstrap()
    assert rerun.returncode != 0 and 'interactive terminal' in rerun.stderr
    with owner_engine.begin() as db: db.execute(text('TRUNCATE users CASCADE'))
    assert 'No platform owner exists.' in run_bootstrap('--status').stdout
    # Standalone import path used by `python -m app.bootstrap`, without app.main loading other models first.
    created = run_bootstrap(script="import os; from sqlalchemy import create_engine; from sqlalchemy.orm import Session; from app import bootstrap; "
        "engine = create_engine(os.environ['MIGRATION_DATABASE_URL'])\nwith Session(engine) as db, db.begin(): bootstrap.create_owner(db, 'fresh-owner', 'Fresh-Owner-Pass-3')")
    assert created.returncode == 0, created.stderr
    assert 'Fresh-Owner-Pass-3' not in created.stdout + created.stderr
    assert 'Platform owner exists: fresh-owner' in run_bootstrap('--status').stdout
