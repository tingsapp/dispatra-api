"""Company-free authentication and per-role contact reserves under real RLS."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
import pytest
from app.main import app
from app.models import User
from app.database import engine
from conftest import login, company, post, PASSWORD, HEADERS, owner_engine
from test_manual_operations import setup, check, BASE


def sign_in(client, portal, email, password=PASSWORD, **extra):
    return client.post('/api/v1/auth/login', json=dict(portal=portal, login_id=email, password=password, **extra))


def test_company_discovery_and_shared_contacts_between_roles(client, setup):
    w = setup
    shipper = check(post(client, BASE + '/shippers', {**w['shipper_body'],
        'email': 'dana@example.com', 'phone': '+1 (604) 555-0102'}), 201)
    with TestClient(app, headers=HEADERS) as browser:
        result = check(sign_in(browser, 'customer', ' DANA@EXAMPLE.COM ', shipper['initial_password']))
        assert result['organization']['slug'] == 'acme' and result['shipper_id'] == shipper['id']
        assert browser.get('/api/v1/companies/other/shipper/profile').status_code == 404
        result = check(sign_in(browser, 'driver', 'dana@example.com', w['driver']['initial_password']))
        assert result['driver_id'] == w['driver']['id']
        assert sign_in(browser, 'dispatch', 'dana@example.com', w['driver']['initial_password']).status_code == 401
        assert sign_in(browser, 'driver', 'dana@example.com', 'wrong').status_code == 401
        assert sign_in(browser, 'driver', 'dana@example.com', w['driver']['initial_password'], organization='other').status_code == 401
        mobile = browser.post('/api/v1/auth/driver/login', json=dict(login_id='dana@example.com', password=w['driver']['initial_password']))
        assert mobile.status_code == 200, mobile.text
        assert mobile.json()['account']['organization']['slug'] == 'acme'
        assert mobile.json()['token'].startswith('dm_')


def test_dispatcher_discovery_shared_email_and_suspension(client):
    login(client); org = company(client)
    assert check(sign_in(client, 'dispatch', 'dispatcher'))['organization']['id'] == org['id']
    shipper = check(post(client, '/api/v1/companies/acme/customers', dict(
        name='Same email', number='S-LEGACY', login_id='dispatcher', password=PASSWORD)), 201)
    assert check(sign_in(client, 'customer', 'dispatcher'))['shipper_id'] == shipper['id']
    with owner_engine.begin() as db:
        db.execute(text('UPDATE organizations SET active=false WHERE id=:id'), {'id': org['id']})
    assert sign_in(client, 'dispatch', 'dispatcher').status_code == 403
    assert sign_in(client, 'dispatch', 'dispatcher', 'wrong').status_code == 401


@pytest.mark.parametrize('kind', ['driver', 'shipper'])
@pytest.mark.parametrize('field', ['email', 'phone'])
def test_same_role_contacts_reserved_across_companies(client, setup, kind, field):
    w = setup
    login(client); company(client, 'other'); login(client, 'dispatch', 'other', 'dispatcher')
    original = w[f'{kind}_body']
    body = {**original, 'email': 'unique@example.com', 'phone': '6045550199',
        field: original['email'].upper() if field == 'email' else '+1 ' + original['phone']}
    body['vehicle_id' if kind == 'driver' else 'rate_card_id'] = None
    path = f'/api/v1/companies/other/{kind}s'
    response = post(client, path, body)
    assert response.status_code == 409, response.text
    assert client.get(path).json() == []


def test_phone_profile_update_is_atomic_and_releases_old_reserve(client, setup):
    w = setup
    check(post(client, BASE + '/drivers', {**w['driver_body'], 'vehicle_id': None,
        'email': 'second@example.com', 'phone': '6045550199'}), 201)
    def update(phone):
        return w['mobile'].patch(BASE + '/driver/profile', json={'version': w['driver']['version'], 'phone': phone},
            headers={'Idempotency-Key': str(uuid4())})
    assert update('+1 604-555-0199').status_code == 409
    assert update('6045550177').status_code == 200
    check(post(client, BASE + '/drivers', {**w['driver_body'], 'vehicle_id': None,
        'email': 'third@example.com', 'phone': '+1 604-555-0102'}), 201)
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT phone FROM users WHERE driver_id=:id'), {'id': w['driver']['id']}) == '6045550177'


def test_login_lookup_policy_read_only_and_transaction_local(client):
    login(client); company(client); company(client, 'other')
    with Session(engine) as db:
        with db.begin():
            assert db.scalars(select(User).where(User.organization_id.is_not(None))).all() == []
            db.execute(text("SELECT set_config('app.login_role', 'DISPATCHER', true), set_config('app.login_id', 'other-dispatcher', true)"))
            rows = db.scalars(select(User)).all()
            assert [row.login_id for row in rows if row.organization_id] == ['other-dispatcher']
            assert db.execute(text("UPDATE users SET active=false WHERE login_id='other-dispatcher'")).rowcount == 0
        db.expunge_all()
        with db.begin():
            assert db.scalars(select(User).where(User.organization_id.is_not(None))).all() == []


def test_concurrent_same_role_contact_reservations_have_one_winner(client):
    login(client); company(client); company(client, 'other')
    cookies = dict(client.cookies)
    def create(slug):
        with TestClient(app, headers=HEADERS, cookies=cookies) as browser:
            login(browser, 'dispatch', slug, 'dispatcher')
            return post(browser, f'/api/v1/companies/{slug}/customers', dict(
                name='Concurrent', number='S-ONE', login_id='global@example.com', phone='6045550177', password=PASSWORD))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ['acme', 'other']))
    assert sorted(r.status_code for r in results) == [201, 409]


def test_dispatcher_phone_reserve_follows_company_contact_and_rolls_back(client):
    login(client)
    first = check(post(client, '/api/v1/platform/organizations', dict(name='First', slug='first',
        admin_login='first@example.com', phone='6045550100', password=PASSWORD)), 201)
    second = post(client, '/api/v1/platform/organizations', dict(name='Second', slug='second',
        admin_login='second@example.com', phone='+1 (604) 555-0100', password=PASSWORD))
    assert second.status_code == 409, second.text
    assert [org['slug'] for org in client.get('/api/v1/platform/organizations').json()] == ['first']
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT phone FROM users WHERE organization_id=:id'), {'id': first['id']}) == '6045550100'


def test_activating_legacy_dispatcher_checks_current_company_phone(client):
    login(client)
    first = check(post(client, '/api/v1/platform/organizations', dict(name='First', slug='first',
        admin_login='first@example.com', phone='6045550100', password=PASSWORD)), 201)
    account = client.get(f"/api/v1/platform/organizations/{first['id']}/dispatchers").json()[0]
    with owner_engine.begin() as db:
        db.execute(text('UPDATE users SET active=false WHERE id=:id'), {'id': account['id']})
        db.execute(text("UPDATE company_settings SET data=jsonb_set(data, '{phone}', '\"6045550101\"'::jsonb) WHERE organization_id=:org"), {'org': first['id']})
    check(post(client, '/api/v1/platform/organizations', dict(name='Second', slug='second',
        admin_login='second@example.com', phone='6045550101', password=PASSWORD)), 201)
    response = post(client, f"/api/v1/platform/organizations/{first['id']}/dispatchers/{account['id']}/activate", {'version': 1})
    assert response.status_code == 409, response.text
    with owner_engine.connect() as db:
        assert not db.scalar(text('SELECT active FROM users WHERE id=:id'), {'id': account['id']})
