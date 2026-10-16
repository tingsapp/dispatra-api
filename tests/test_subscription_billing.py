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
        db.execute(text("UPDATE subscription_plans SET monthly_price=NULL, currency='CAD', dispatch_monthly_price=NULL, dispatch_currency='CAD', version=1, updated_at=now()"))


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
    expected = {'name': 'Monthly subscription', 'interval': 'MONTHLY', 'trial_days': 30, 'trial_enabled': True,
                'monthly_price': None, 'currency': 'CAD', 'dispatch_monthly_price': None, 'dispatch_currency': 'CAD'}
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
    {'dispatch_monthly_price': '-1'}, {'dispatch_monthly_price': '1.001'}, {'dispatch_monthly_price': 'NaN'},
    {'dispatch_monthly_price': '10000000000'}, {'dispatch_currency': 'usd'}, {'dispatch_currency': None},
    {'trial_days': 0}, {'trial_days': 366}, {'trial_days': 1.5}, {'trial_days': None}, {'trial_enabled': None}, {'trial_enabled': 'yes'}, {'interval': 'YEARLY'}, {'version': 0},
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


def test_two_plan_terms_are_exact_public_independent_and_legacy_updates_preserve_dispatch(client):
    login(client)
    body = dict(version=1, monthly_price='99.95', currency='CAD', dispatch_monthly_price='29.90', dispatch_currency='USD')
    key = str(uuid4())
    first = patch(client, body, key)
    assert first.status_code == 200, first.text
    assert patch(client, body, key).json() == first.json()
    assert patch(client, {**body, 'dispatch_monthly_price': '30.00'}, key).status_code == 409
    public = client.get('/api/v1/subscription/plan').json()
    assert (public['monthly_price'], public['currency'], public['dispatch_monthly_price'], public['dispatch_currency']) == ('99.95', 'CAD', '29.90', 'USD')
    legacy = patch(client, dict(version=2, monthly_price='89.00', currency='EUR'))
    assert legacy.status_code == 200
    assert legacy.json()['dispatch_monthly_price'] == '29.90' and legacy.json()['dispatch_currency'] == 'USD'
    cleared = patch(client, dict(version=3, monthly_price='89.00', currency='EUR', dispatch_monthly_price=None))
    assert cleared.status_code == 200 and cleared.json()['dispatch_monthly_price'] is None
    assert cleared.json()['dispatch_currency'] == 'USD'


@pytest.mark.parametrize('external', [False, True])
def test_owner_and_trial_creation_snapshot_the_selected_plan_without_repricing_existing_companies(client, external):
    login(client)
    body = dict(version=1, monthly_price='99.95', currency='CAD', dispatch_monthly_price='29.90', dispatch_currency='USD')
    assert patch(client, body).status_code == 200
    response = client.post('/api/v1/platform/organizations', json=dict(slug='plan-owner', name='Owner company',
        admin_login='plan-owner@example.com', password=PASSWORD, tms_workspace=external), headers={'Idempotency-Key':str(uuid4())})
    assert response.status_code == 201, response.text
    owner_id = response.json()['id']
    with TestClient(app, headers=HEADERS) as signup:
        result = signup.post('/api/v1/auth/signup', json=dict(slug='plan-trial', name='Trial company', full_name='Dispatcher',
            email='plan-trial@example.com', password=PASSWORD, tms_workspace=external), headers={'Idempotency-Key':str(uuid4())})
        assert result.status_code == 201, result.text
        verified = confirm_trial(signup, 'plan-trial@example.com')
        assert verified.status_code == 200
        trial_id = verified.json()['organization']['id']
    expected = ('29.90', 'USD') if external else ('99.95', 'CAD')
    for ident in [owner_id,trial_id]:
        saved = client.get(f'/api/v1/platform/organizations/{ident}/subscription').json()
        assert (saved['monthly_price'], saved['currency']) == expected
    assert patch(client, {**body,'version':2,'monthly_price':'500.00','dispatch_monthly_price':'250.00'}).status_code == 200
    for ident in [owner_id,trial_id]:
        saved = client.get(f'/api/v1/platform/organizations/{ident}/subscription').json()
        assert (saved['monthly_price'], saved['currency']) == expected


def test_dispatch_price_accepts_zero_and_cannot_be_changed_without_owner(client):
    login(client)
    assert patch(client, dict(version=1,monthly_price=None,currency='CAD',dispatch_monthly_price='0.00',dispatch_currency='USD')).status_code == 200
    company(client)
    login(client,'dispatch','acme','dispatcher')
    assert patch(client,dict(version=2,monthly_price=None,currency='CAD',dispatch_monthly_price='1.00',dispatch_currency='USD')).status_code == 403
    public=client.get('/api/v1/subscription/plan').json()
    assert public['dispatch_monthly_price']=='0.00'
    with Session(engine) as db, db.begin():
        context(db)
        assert db.execute(text('UPDATE subscription_plans SET dispatch_monthly_price=5')).rowcount == 0


def test_trial_settings_persist_publicly_and_legacy_price_updates_keep_them(client):
    login(client)
    body=dict(version=1,monthly_price='49.95',currency='CAD',trial_enabled=False,trial_days=14)
    key=str(uuid4());saved=patch(client,body,key)
    assert saved.status_code==200,saved.text
    assert not saved.json()['trial_enabled'] and saved.json()['trial_days']==14
    assert patch(client,body,key).json()==saved.json()
    assert patch(client,{**body,'trial_days':21},key).status_code==409
    legacy=patch(client,dict(version=2,monthly_price='60.00',currency='CAD'))
    assert legacy.status_code==200 and not legacy.json()['trial_enabled'] and legacy.json()['trial_days']==14
    public=client.get('/api/v1/subscription/plan').json()
    assert not public['trial_enabled'] and public['trial_days']==14
    with TestClient(app,headers=HEADERS) as anonymous:
        result=anonymous.post('/api/v1/auth/signup',json=dict(slug='disabled-trial',name='Trial',full_name='Dispatcher',email='disabled@example.com',password=PASSWORD),headers={'Idempotency-Key':str(uuid4())})
        assert result.status_code==409
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations'))==0
    enabled=patch(client,dict(version=3,monthly_price='60.00',currency='CAD',trial_enabled=True))
    assert enabled.status_code==200 and enabled.json()['trial_enabled'] and enabled.json()['trial_days']==14


def test_trial_duration_is_accepted_at_signup_and_preserved_before_and_after_verification(client):
    from datetime import datetime
    login(client)
    assert patch(client,dict(version=1,monthly_price='49.95',currency='CAD',trial_days=14)).status_code==200
    with TestClient(app,headers=HEADERS) as signup:
        key=str(uuid4());body=dict(slug='accepted-trial',name='Trial',full_name='Dispatcher',email='accepted@example.com',password=PASSWORD)
        first=signup.post('/api/v1/auth/signup',json=body,headers={'Idempotency-Key':key})
        assert first.status_code==201,first.text
        assert patch(client,dict(version=2,monthly_price='49.95',currency='CAD',trial_enabled=False,trial_days=7)).status_code==200
        # Replaying an already accepted signup still permits email verification.
        assert signup.post('/api/v1/auth/signup',json=body,headers={'Idempotency-Key':key}).status_code==201
        verified=confirm_trial(signup,'accepted@example.com')
        assert verified.status_code==200,verified.text
        org=verified.json()['organization']
        assert (datetime.fromisoformat(org['trial_ends_at'])-datetime.fromisoformat(org['trial_started_at'])).days==14
    assert patch(client,dict(version=3,monthly_price='49.95',currency='CAD',trial_enabled=True,trial_days=90)).status_code==200
    detail=client.get('/api/v1/platform/organizations/'+org['id']).json()
    assert detail['trial_ends_at']==org['trial_ends_at']


def test_trial_settings_are_owner_only_and_client_cannot_override_duration(client):
    login(client);company(client)
    login(client,'dispatch','acme','dispatcher')
    assert patch(client,dict(version=1,monthly_price=None,currency='CAD',trial_enabled=False,trial_days=14)).status_code==403
    with Session(engine) as db,db.begin():
        context(db)
        assert db.execute(text('UPDATE subscription_plans SET trial_enabled=false,trial_days=1')).rowcount==0
    with TestClient(app,headers=HEADERS) as signup:
        result=signup.post('/api/v1/auth/signup',json=dict(slug='bad-duration',name='Trial',full_name='Dispatcher',email='override@example.com',password=PASSWORD,trial_days=365),headers={'Idempotency-Key':str(uuid4())})
        assert result.status_code==422


def test_trial_settings_migration_preserves_pending_promises_and_recorded_dates(client):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.models import now
    from datetime import timedelta
    login(client);org=company(client)
    start=now()-timedelta(days=2);end=start+timedelta(days=30)
    pending=str(uuid4())
    module_path=Path(__file__).parents[1]/'migrations/versions/0054_trial_settings.py'
    spec=importlib.util.spec_from_file_location('trial_settings_migration',module_path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with owner_engine.begin() as db:
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
            db.execute(text('UPDATE organizations SET trial_started_at=:start,trial_ends_at=:end WHERE id=:id'),dict(start=start,end=end,id=org['id']))
            db.execute(text("INSERT INTO organizations (id,slug,name,active,version,created_at,updated_at,verification_pending) VALUES (:id,'pending-migration','Pending',true,1,now(),now(),true)"),{'id':pending})
            migration.upgrade()
        assert db.execute(text('SELECT trial_enabled,trial_days FROM subscription_plans')).one()==(True,30)
        assert db.scalar(text('SELECT trial_duration_days FROM organizations WHERE id=:id'),{'id':pending})==30
        assert db.execute(text('SELECT trial_started_at,trial_ends_at FROM organizations WHERE id=:id'),{'id':org['id']}).one()==(start,end)
