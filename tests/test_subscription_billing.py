"""SaaS plan configuration through owner authorization and actual PostgreSQL RLS."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from conftest import login, company, customer, HEADERS, PASSWORD, owner_engine, confirm_trial

pytestmark = pytest.mark.usefixtures('verification_mail')
from app.main import app
from app.database import engine, context
from sqlalchemy.orm import Session

PATH = '/api/v1/platform/billing/plan'


@pytest.fixture(autouse=True)
def reset_plan():
    with owner_engine.begin() as db:
        db.execute(text("UPDATE subscription_plans SET monthly_price=NULL, currency='CAD', version=1, updated_at=now()"))


def patch(client, body, key=None):
    return client.patch(PATH, json=body, headers={'Idempotency-Key': key or str(uuid4())})


def test_owner_only_and_database_scope(client):
    assert client.get(PATH).status_code == 401
    login(client)
    plan = client.get(PATH).json()
    assert plan['interval'] == 'MONTHLY' and plan['trial_days'] == 30
    assert plan['monthly_price'] is None and plan['currency'] == 'CAD'
    company(client)
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(PATH).status_code == 403
    assert patch(client, dict(version=1, monthly_price='29.99', currency='CAD')).status_code == 403
    customer(client)
    login(client, 'customer', 'acme', 'customer')
    assert client.get(PATH).status_code == 403
    assert patch(client, dict(version=1, monthly_price='29.99', currency='CAD')).status_code == 403
    with Session(engine) as db, db.begin():
        context(db)
        assert db.scalar(text('SELECT count(*) FROM subscription_plans')) == 1
        assert db.execute(text('UPDATE subscription_plans SET monthly_price=1')).rowcount == 0
        context(db, platform=True)
        assert db.scalar(text('SELECT count(*) FROM subscription_plans')) == 1


def test_public_plan_exposes_terms_only_and_reflects_owner_price(client):
    public = '/api/v1/subscription/plan'
    expected = {'name': 'Monthly subscription', 'interval': 'MONTHLY', 'trial_days': 30,
                'monthly_price': None, 'currency': 'CAD'}
    assert client.get(public).json() == expected
    assert patch(client, dict(version=1, monthly_price='1', currency='USD')).status_code == 401
    assert client.patch(public, json={'monthly_price': '1'}).status_code == 405
    login(client)
    assert patch(client, dict(version=1, monthly_price='49.95', currency='USD')).status_code == 200
    with TestClient(app, headers=HEADERS) as anonymous:
        response = anonymous.get(public)
        assert response.status_code == 200
        assert response.json() == {**expected, 'monthly_price': '49.95', 'currency': 'USD'}
        assert anonymous.get(PATH).status_code == 401


def test_money_persistence_versions_retry_and_private_audit(client):
    login(client)
    body = dict(version=1, monthly_price='49.95', currency='USD')
    key = str(uuid4())
    first = patch(client, body, key)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved['monthly_price'] == '49.95' and saved['version'] == 2
    loaded = client.get(PATH).json()
    from datetime import datetime
    assert datetime.fromisoformat(loaded.pop('updated_at')) == datetime.fromisoformat(saved['updated_at'])
    assert loaded == {key: value for key, value in saved.items() if key != 'updated_at'}
    assert patch(client, body, key).json() == saved
    assert patch(client, body).status_code == 409
    assert patch(client, {**body, 'monthly_price': '50'}, key).status_code == 409
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='subscription.plan_updated'")) == 1
        assert db.scalar(text('SELECT count(*) FROM notifications')) == 0
    unset = patch(client, dict(version=2, monthly_price=None, currency='CAD'))
    assert unset.status_code == 200 and unset.json()['monthly_price'] is None


@pytest.mark.parametrize('fields', [
    {'monthly_price': '-1'}, {'monthly_price': '1.001'}, {'monthly_price': '10000000000'},
    {'monthly_price': 'NaN'}, {'currency': 'usd'}, {'currency': 'US'},
    {'trial_days': 90}, {'interval': 'YEARLY'}, {'version': 0},
])
def test_invalid_plan_changes_rejected(client, fields):
    login(client)
    result = patch(client, {**dict(version=1, monthly_price='49.95', currency='CAD'), **fields})
    assert result.status_code == 422, result.text
    assert client.get(PATH).json()['version'] == 1


def test_signup_uses_plan_trial_without_price_or_access_changes(client):
    login(client)
    assert patch(client, dict(version=1, monthly_price='49.95', currency='USD')).status_code == 200
    with TestClient(app, headers=HEADERS) as signup:
        response = signup.post('/api/v1/auth/signup', json=dict(slug='new-trial', name='New Trial', full_name='Trial Dispatcher', email='new-trial@example.com', password=PASSWORD), headers={'Idempotency-Key': str(uuid4())})
        assert response.status_code == 201, response.text
        assert response.json()['verification_required']
        verified = confirm_trial(signup, 'new-trial@example.com')
        assert verified.status_code == 200
        org = verified.json()['organization']
    from datetime import datetime
    assert (datetime.fromisoformat(org['trial_ends_at']) - datetime.fromisoformat(org['trial_started_at'])).days == client.get(PATH).json()['trial_days']
    assert patch(client, dict(version=2, monthly_price='60', currency='CAD')).status_code == 200
    detail = client.get('/api/v1/platform/organizations/' + org['id']).json()
    assert detail['trial_ends_at'] == org['trial_ends_at'] and detail['active']


def test_concurrent_updates_only_one_version_wins(client):
    login(client)
    barrier = Barrier(2)
    def update(price):
        with TestClient(app, headers=HEADERS) as owner:
            login(owner)
            barrier.wait(timeout=15)
            return patch(owner, dict(version=1, monthly_price=price, currency='CAD')).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(update, ['40', '50']))
    assert sorted(responses) == [200, 409]
    assert client.get(PATH).json()['version'] == 2
