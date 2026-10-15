"""Independent SaaS agreements and revenue with restricted PostgreSQL permissions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conftest import login, company, customer, post, owner_engine, HEADERS, PASSWORD, confirm_trial
from app.main import app
from app.database import engine, context
from app.models import now
from app.operations.models import Catalog, Vehicle

BASE = '/api/v1/platform'


def path(org):
    return f"{BASE}/organizations/{org['id']}/subscription"


def terms(**overrides):
    return dict(version=1, status='ACTIVE', monthly_price='49.95', currency='CAD', starts_on='2026-01-31', **overrides)


def save(client, org, body=None, key=None):
    return client.patch(path(org), json=body or terms(), headers={'Idempotency-Key': key or str(uuid4())})


def fleet(org, count, active=True, archived=False):
    with Session(owner_engine) as db, db.begin():
        organization_id = UUID(org['id'])
        type_id = db.scalar(select(Catalog.id).where(Catalog.organization_id == organization_id, Catalog.code == 'veh_1_ton'))
        rows = [Vehicle(organization_id=organization_id, type_id=type_id, number=f'DAV-{uuid4()}',
            plate=str(uuid4())[:8], province='BC', active=active, archived_at=now() if archived else None,
            data={'unit_number': str(uuid4())}) for _ in range(count)]
        db.add_all(rows)
        db.flush()
        return [row.id for row in rows]


@pytest.fixture(autouse=True)
def reset_plan():
    with owner_engine.begin() as db:
        db.execute(text("UPDATE subscription_plans SET monthly_price=NULL, currency='CAD', version=1"))


def test_owner_authorization_and_database_isolation(client):
    assert client.get(BASE + '/billing/revenue').status_code == 401
    assert client.get(BASE + '/billing/subscriptions').status_code == 401
    login(client); org = company(client); foreign = company(client, 'other')
    assert client.get(path(org)).json()['status'] == 'NOT_SUBSCRIBED'
    assert client.get(BASE + '/billing/revenue').json()['active_subscriptions'] == 0
    for role in ['dispatch', 'customer']:
        login(client, 'dispatch', 'acme', 'dispatcher')
        if role == 'customer':
            customer(client); login(client, 'customer', 'acme', 'customer')
        assert client.get(path(org)).status_code == 403
        assert save(client, foreign).status_code == 403
        assert client.get(BASE + '/billing/subscriptions').status_code == 403
        assert client.get(BASE + '/billing/revenue').status_code == 403
    with Session(engine) as db, db.begin():
        context(db, org['id'])
        assert db.scalar(text('SELECT count(*) FROM company_subscriptions')) == 1
        assert db.scalar(text('SELECT organization_id FROM company_subscriptions')) == UUID(org['id'])
        assert db.execute(text("UPDATE company_subscriptions SET status='CANCELLED'")).rowcount == 0
        context(db, platform=True)
        assert db.scalar(text('SELECT count(*) FROM company_subscriptions')) == 2
    login(client)
    missing = {'id': str(uuid4())}
    assert client.get(path(missing)).status_code == 404
    assert save(client, missing).status_code == 404


def test_default_snapshots_independent_changes_replay_and_audit(client):
    login(client)
    changed = client.patch(BASE + '/billing/plan', json=dict(version=1, monthly_price='29.99', currency='USD'), headers={'Idempotency-Key': str(uuid4())})
    assert changed.status_code == 200
    first = company(client); second = company(client, 'other')
    assert client.get(path(first)).json()['monthly_price'] == '29.99'
    original = client.get(path(second)).json()
    key = str(uuid4()); body = terms()
    saved = save(client, first, body, key)
    assert saved.status_code == 200, saved.text
    assert saved.json()['monthly_price'] == '49.95' and saved.json()['version'] == 2
    assert save(client, first, body, key).json() == saved.json()
    assert save(client, first, body).status_code == 409
    assert save(client, first, {**body, 'monthly_price': '50'}, key).status_code == 409
    assert client.get(path(second)).json() == original
    client.patch(BASE + '/billing/plan', json=dict(version=2, monthly_price='70', currency='CAD'), headers={'Idempotency-Key': str(uuid4())})
    assert client.get(path(second)).json() == original
    # Access activation/suspension neither starts nor cancels a subscription.
    detail = client.get(f"{BASE}/organizations/{first['id']}").json()
    assert post(client, f"{BASE}/organizations/{first['id']}/suspend", {'version': detail['version']}).status_code == 200
    assert client.get(path(first)).json()['status'] == 'ACTIVE'
    history = client.get(f"{BASE}/organizations/{first['id']}/audit").json()
    assert sum(row['action'] == 'subscription.company_updated' for row in history) == 1
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM events WHERE type='subscription.company_updated'")) == 0


@pytest.mark.parametrize('override', [
    {'monthly_price': None}, {'starts_on': None}, {'monthly_price': '-1'}, {'monthly_price': '1.001'},
    {'monthly_price': 'NaN'}, {'monthly_price': '10000000000'}, {'currency': 'usd'}, {'currency': 'US'},
    {'status': 'PAID'}, {'starts_on': '2026-02-30'}, {'version': 0}, {'organization_id': str(uuid4())},
])
def test_invalid_terms_rollback(client, override):
    login(client); org = company(client)
    response = save(client, org, {**terms(), **override})
    assert response.status_code == 422, response.text
    assert client.get(path(org)).json()['version'] == 1


def test_exact_revenue_by_currency_status_date_and_all_pages(client):
    login(client)
    orgs = [company(client, slug) for slug in ('acme', 'other', 'paused', 'cancelled', 'future', 'free', 'unconfigured')]
    inputs = [terms(), {**terms(), 'monthly_price': '10.01', 'currency': 'USD'},
        {**terms(), 'status': 'PAUSED'}, {**terms(), 'status': 'CANCELLED'},
        {**terms(), 'starts_on': (date.today() + timedelta(days=2)).isoformat()}, {**terms(), 'monthly_price': '0.00'}]
    for org, body in zip(orgs, inputs): assert save(client, org, body).status_code == 200
    fleet(orgs[0], 1)
    fleet(orgs[0], 1, active=False)
    fleet(orgs[0], 1, archived=True)
    fleet(orgs[1], 3)
    for org in orgs[2:]: fleet(org, 5)
    revenue = client.get(BASE + '/billing/revenue').json()
    assert revenue['total_companies'] == 7 and revenue['active_subscriptions'] == 3
    assert revenue['scheduled_subscriptions'] == 1 and revenue['paused_subscriptions'] == 1
    assert revenue['cancelled_subscriptions'] == 1 and revenue['not_subscribed'] == 1
    assert revenue['currencies'] == [
        dict(currency='CAD', monthly_recurring_revenue='99.90', annualized_revenue='1198.80', active_subscriptions=2),
        dict(currency='USD', monthly_recurring_revenue='30.03', annualized_revenue='360.36', active_subscriptions=1)]
    first = client.get(BASE + '/billing/subscriptions?limit=2').json()
    assert len(first) == 2
    second = client.get(BASE + '/billing/subscriptions', params={'limit': 100, 'after': first[-1]['organization_id']}).json()
    assert len(second) == 5 and len({row['organization_id'] for row in first + second}) == 7
    assert client.get(BASE + '/billing/revenue').json() == revenue
    assert len(client.get(BASE + '/billing/subscriptions?search=acme&status=ACTIVE').json()) == 1
    assert client.get(BASE + '/billing/subscriptions?search=%25').json() == []
    assert client.get(BASE + '/billing/subscriptions?status=PAID').status_code == 422


def test_revenue_tracks_registered_fleet_without_changing_subscription(client):
    login(client); org = company(client)
    assert save(client, org, {**terms(), 'monthly_price': '9999999999.99'}).status_code == 200
    agreement = client.get(path(org)).json()
    def monthly():
        result = client.get(BASE + '/billing/revenue')
        assert result.status_code == 200, result.text
        return result.json()['currencies'][0]
    assert monthly()['monthly_recurring_revenue'] == '0.00'
    ids = fleet(org, 3)
    assert monthly()['monthly_recurring_revenue'] == '29999999999.97'
    assert monthly()['annualized_revenue'] == '359999999999.64'
    with owner_engine.begin() as db:
        db.execute(text('UPDATE vehicles SET archived_at=now() WHERE id=:id'), {'id': ids[0]})
    assert monthly()['monthly_recurring_revenue'] == '19999999999.98'
    assert client.get(path(org)).json() == agreement


def test_concurrent_company_subscription_updates(client):
    login(client); org = company(client)
    barrier = Barrier(2)
    def update(price):
        with TestClient(app, headers=HEADERS) as owner:
            login(owner); barrier.wait(timeout=15)
            return save(owner, org, {**terms(), 'monthly_price': price}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ['40.01', '50.02'])) == [200, 409]
    assert client.get(path(org)).json()['version'] == 2


def test_verified_trial_remains_outside_revenue(client, verification_mail):
    with TestClient(app, headers=HEADERS) as signup:
        created = post(signup, '/api/v1/auth/signup', dict(slug='trial', name='Trial', full_name='Trial Owner', email='trial@example.com', password=PASSWORD))
        assert created.status_code == 201, created.text
        assert confirm_trial(signup, 'trial@example.com').status_code == 200
    login(client)
    revenue = client.get(BASE + '/billing/revenue').json()
    assert revenue['total_companies'] == 1 and revenue['active_subscriptions'] == 0 and revenue['currencies'] == []
    row = client.get(BASE + '/billing/subscriptions').json()[0]
    assert row['status'] == 'NOT_SUBSCRIBED' and row['trial_ends_at'] is not None and row['workspace_active']



def test_migration_backfill_preserves_access_and_snapshot_defaults(client):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    login(client); org = company(client)
    before = client.get(f"{BASE}/organizations/{org['id']}").json()
    migration_path = Path(__file__).parents[1] / 'migrations/versions/0046_company_subscriptions.py'
    spec = importlib.util.spec_from_file_location('company_subscription_migration', migration_path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    with owner_engine.connect() as db:
        transaction = db.begin()
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
            db.execute(text("UPDATE subscription_plans SET monthly_price=12.34, currency='USD'"))
            migration.upgrade()
        row = db.execute(text('SELECT * FROM company_subscriptions')).mappings().one()
        assert str(row['organization_id']) == org['id'] and row['status'] == 'NOT_SUBSCRIBED'
        assert str(row['monthly_price']) == '12.34' and row['currency'] == 'USD'
        assert row['starts_on'] is None and row['version'] == 1
        # Restore the fully migrated schema, including policies added after 0046.
        transaction.rollback()
    assert client.get(f"{BASE}/organizations/{org['id']}").json() == before
    assert client.get(BASE + '/billing/revenue').json()['currencies'] == []
