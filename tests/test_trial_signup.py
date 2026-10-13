"""Public registration and 30-day access enforcement on restricted PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.main import app
from app.models import Organization, User, now
from app.database import engine, context
from app.security import verify
from conftest import PASSWORD, HEADERS, owner_engine, login, company, post, confirm_trial

pytestmark = pytest.mark.usefixtures('verification_mail')

PATH = '/api/v1/auth/signup'
BODY = dict(slug='trial-company', name='Trial Company', full_name='Trial Dispatcher',
    email='trial@example.com', phone='6045550198', password=PASSWORD)


def signup(client, body=None, key=None):
    data = body or BODY
    response = post(client, PATH, data, key)
    if response.status_code == 201 and response.json().get('verification_required'):
        return confirm_trial(client, data['email'])
    return response


def test_signup_creates_isolated_dispatcher_and_thirty_day_defaults(client):
    response = signup(client, {**BODY, 'email': 'TRIAL@EXAMPLE.COM'})
    assert response.status_code == 200, response.text
    account = response.json(); org = account['organization']
    assert account['role'] == 'DISPATCHER' and account['login_id'] == BODY['email']
    assert org['slug'] == BODY['slug'] and org['active']
    assert org['trial_started_at'] and org['trial_ends_at'] and org['trial_activated_at'] is None
    assert client.get('/api/v1/auth/me').json()['id'] == account['id']
    assert client.get('/api/v1/platform/organizations').status_code == 403
    assert client.get('/api/v1/companies/other/settings').status_code == 404
    base = '/api/v1/companies/trial-company'
    settings = client.get(base + '/settings').json()['data']
    assert settings['company_name'] == BODY['name'] and settings['contact_name'] == BODY['full_name']
    cards = client.get(base + '/rate-cards').json()
    assert {card['data']['method'] for card in cards if card['active']} == {'BASE_PLUS_DISTANCE', 'FIXED', 'ZONE', 'HOURLY', 'IMPORTED'}
    with owner_engine.connect() as db:
        dates = db.execute(text('SELECT trial_started_at, trial_ends_at FROM organizations WHERE id=:id'), {'id':org['id']}).one()
        assert dates[1] - dates[0] == timedelta(days=30)
        assert verify(PASSWORD, db.scalar(text('SELECT password_hash FROM users WHERE id=:id'), {'id': account['id']}))
        assert PASSWORD not in str(db.execute(text('SELECT result FROM operations')).all())
        assert 'organization.trial_started' in db.execute(text('SELECT action FROM audit_events')).scalars().all()
    with Session(engine) as db, db.begin():
        assert db.scalars(select(User).where(User.organization_id.is_not(None))).all() == []
        context(db, org['id'])
        assert [str(row.id) for row in db.scalars(select(User).where(User.organization_id.is_not(None)))] == [account['id']]


def test_retry_does_not_restart_trial_and_old_password_cannot_authenticate_after_rotation(client):
    key = str(uuid4())
    first = signup(client, key=key)
    assert first.status_code == 200, first.text
    repeat = signup(client, key=key)
    assert repeat.status_code == 201 and repeat.json() == first.json()
    assert signup(client, {**BODY, 'name': 'Changed'}, key).status_code == 409
    assert client.post('/api/v1/auth/password', json={'current_password':PASSWORD, 'new_password':'Changed-password-123!'}).status_code == 204
    assert signup(client, key=key).status_code == 401
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations')) == 1


def test_signup_address_is_saved_to_company_settings_and_payload_bound(client):
    key = str(uuid4())
    address = '1420 Derwent Way, Delta, BC V3M 6M7, Canada'
    body = {**BODY, 'address': '  ' + address + '  '}
    first = signup(client, body, key)
    assert first.status_code == 200, first.text
    settings = client.get('/api/v1/companies/trial-company/settings').json()['data']
    assert settings['address'] == address
    assert signup(client, {**BODY, 'address': address}, key).json() == first.json()
    assert signup(client, {**BODY, 'address': 'Another address'}, key).status_code == 409
    assert client.get('/api/v1/companies/trial-company/settings').json()['data']['address'] == address


def test_signup_blank_address_preserves_legacy_retry_payload(client):
    key = str(uuid4())
    first = signup(client, key=key)
    assert first.status_code == 200
    assert signup(client, {**BODY, 'address': '  '}, key).json() == first.json()
    assert client.get('/api/v1/companies/trial-company/settings').json()['data']['address'] is None
    with owner_engine.connect() as db:
        import json
        stored = db.scalar(text("SELECT payload_hash FROM operations WHERE key LIKE '%:signup:%'"))
        assert verify(json.dumps(BODY, sort_keys=True), stored)


def test_signup_address_length_is_validated_before_provisioning(client):
    assert signup(client, {**BODY, 'address': 'x' * 501}).status_code == 422
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations')) == 0


@pytest.mark.parametrize('field', ['slug', 'email', 'phone'])
def test_duplicate_company_or_dispatcher_contacts_leave_no_partial_workspace(client, field):
    assert signup(client).status_code == 200
    body = {**BODY, 'slug':'another-trial', 'email':'another@example.com', 'phone':'6045550177', field:BODY[field]}
    response = signup(client, body)
    assert response.status_code == 409, response.text
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations')) == 1
        assert db.scalar(text("SELECT count(*) FROM users WHERE role='DISPATCHER'")) == 1


@pytest.mark.parametrize('extra', [{'role':'ADMIN'}, {'organization_id':str(uuid4())}, {'trial_ends_at':'2099-01-01T00:00:00Z'}, {'slug':'admin'}, {'password':'short'}])
def test_signup_cannot_choose_privilege_existing_tenant_or_trial_period(client, extra):
    response = signup(client, {**BODY, **extra})
    assert response.status_code == 422, response.text
    assert client.get('/api/v1/auth/me').status_code == 401
    assert PASSWORD not in response.text


def test_expired_trial_blocks_sessions_login_api_keys_and_worker_filters_until_owner_activation(client):
    account = signup(client).json(); org = account['organization']
    base = '/api/v1/companies/trial-company'
    api_key = post(client, base + '/integrations/api-key', {}).json()['key']
    from test_manual_operations import address
    driver = post(client, base + '/drivers', {'name':'Trial Driver','email':'driver@example.com','phone':'6045550102','address':address()}).json()
    with TestClient(app, headers=HEADERS) as mobile:
        login(mobile, 'driver', login_id=driver['email'], password=driver['initial_password'])
        assert post(mobile, base + '/driver/duty', {}).status_code == 201
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET trial_started_at=now()-interval '31 days', trial_ends_at=now()-interval '1 day' WHERE id=:id"), {'id':org['id']})
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.post('/api/v1/auth/login', json={'portal':'dispatch','login_id':BODY['email'],'password':PASSWORD}).status_code == 403
    with TestClient(app, headers={'Authorization':'Bearer '+api_key}) as key_client:
        assert key_client.get(base + '/catalog').status_code == 401
    with Session(owner_engine) as db:
        assert db.scalars(select(Organization).where(Organization.active.is_(True))).all() == []
        assert not db.get(Organization, org['id']).active
    login(client)
    detail = client.get('/api/v1/platform/organizations/' + org['id']).json()
    assert detail['active'] is False
    assert client.get('/api/v1/platform/organizations', params={'status':'ACTIVE'}).json() == []
    assert len(client.get('/api/v1/platform/organizations', params={'status':'SUSPENDED'}).json()) == 1
    activate = post(client, f"/api/v1/platform/organizations/{org['id']}/activate", {'version':detail['version']})
    assert activate.status_code == 200, activate.text
    assert activate.json()['active'] and activate.json()['trial_activated_at']
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT ended_at FROM duty_sessions WHERE driver_id=:id'), {'id':driver['id']}) is not None
    login(client, 'dispatch', login_id=BODY['email'])
    assert client.get(base + '/settings').status_code == 200


def test_trial_session_expires_at_cutoff_without_extending_trial(client):
    account = signup(client).json()
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET trial_started_at=now()-interval '29 days', trial_ends_at=now()+interval '1 hour' WHERE id=:id"), {'id':account['organization']['id']})
    login(client, 'dispatch', login_id=BODY['email'])
    with owner_engine.connect() as db:
        expires, cutoff = db.execute(text('SELECT s.expires_at, o.trial_ends_at FROM login_sessions s JOIN organizations o ON o.id=s.organization_id ORDER BY s.expires_at LIMIT 1')).one()
        assert expires == cutoff


def test_concurrent_signup_same_email_has_one_committed_workspace(client):
    def create(index):
        with TestClient(app, headers=HEADERS) as browser:
            return signup(browser, {**BODY, 'slug':f'trial-{index}', 'phone':''})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, [1, 2]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations')) == 1


def test_signup_keeps_browser_origin_checks_even_with_bearer_headers(client):
    for headers in ({'Origin':'https://attacker.example'}, {'Origin':'', 'Authorization':'Bearer dsp_fake'}):
        assert client.post(PATH, json=BODY, headers={**headers, 'Idempotency-Key':str(uuid4())}).status_code == 403
