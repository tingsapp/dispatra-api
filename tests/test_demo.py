"""Demo provisioning uses real PostgreSQL; HTTP checks use restricted runtime credentials."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.demo import seed_demo, DEMO_ID, DISPATCHER_ID, ADMIN_ID
from app.main import app
from app.models import Organization, User, Operation
from app.operations.models import Driver, Shipper, Vehicle, RateCard, Catalog, DutySession
from app.security import hash_password, verify
from conftest import HEADERS, owner_engine, login


def seed():
    with Session(owner_engine) as db, db.begin():
        return seed_demo(db)


def remove_fixture_admin():
    with Session(owner_engine) as db, db.begin():
        db.execute(delete(User).where(User.role == 'ADMIN'))


def test_demo_logins_pricing_and_repeat_preserve_edits():
    remove_fixture_admin()
    initial = seed()
    assert initial['pricing']['created'] == 32
    assert all(account['initial_password'] == '123456' for account in initial['accounts'])
    for account in initial['accounts']:
        with TestClient(app, headers=HEADERS) as client:
            if account['role'] == 'ADMIN':
                result = login(client, 'platform', None, account['login_id'], account['initial_password'])
                assert result['role'] == 'ADMIN' and result['organization'] is None
                assert account['login_id'] == 'admin@example.com' and account['path'] == '/admin'
                assert client.get('/api/v1/platform/organizations').status_code == 200
                assert client.get('/api/v1/companies/demo/drivers').status_code == 404
                continue
            result = login(client, account['portal'], 'demo', account['login_id'], account['initial_password'])
            assert result['role'] == account['role']
            assert result['organization']['slug'] == 'demo'
            base = '/api/v1/companies/demo'
            if account['role'] == 'DISPATCHER':
                assert len(client.get(base+'/rate-cards').json()) == 6
                assert client.get(base+'/drivers').status_code == 200
            elif account['role'] == 'SHIPPER':
                assert client.get(base+'/shipper/profile').status_code == 200
                assert client.get(base+'/drivers').status_code == 403
            else:
                assert client.get(base+'/driver/profile').status_code == 200
                assert client.get(base+'/invoices').status_code == 403
            assert client.get('/api/v1/companies/another/monitor').status_code == 404
    changed_password = 'Demo-updated-password-123!'
    with Session(owner_engine) as db, db.begin():
        dispatcher = db.get(User, DISPATCHER_ID)
        dispatcher.password_hash = hash_password(changed_password)
        db.get(User, ADMIN_ID).password_hash = hash_password(changed_password)
        driver = db.scalar(select(Driver))
        driver.name = 'Edited Demo Driver'
        shipper = db.scalar(select(Shipper))
        shipper.company_name = 'Edited Demo Company'
        rate = db.scalar(select(RateCard).where(RateCard.code == 'NBH-DIRECT'))
        rate.data = {**rate.data, 'fixed_amount': '123'}
    replay = seed()
    assert replay['pricing']['created'] == 0
    assert all(a['initial_password'] is None for a in replay['accounts'])
    with Session(owner_engine) as db:
        for model, count in [(Driver,1),(Shipper,1),(Vehicle,1),(RateCard,6),(Catalog,26),(DutySession,0)]:
            assert db.scalar(select(func.count()).select_from(model)) == count
        assert db.scalar(select(func.count()).select_from(User).where(User.organization_id == DEMO_ID)) == 3
        assert db.scalar(select(Driver)).name == 'Edited Demo Driver'
        assert db.scalar(select(Shipper)).company_name == 'Edited Demo Company'
        assert db.scalar(select(RateCard).where(RateCard.code == 'NBH-DIRECT')).data['fixed_amount'] == '123'
        assert verify(changed_password, db.get(User, DISPATCHER_ID).password_hash)
        assert verify(changed_password, db.get(User, ADMIN_ID).password_hash)
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'ADMIN')) == 1
        operations = str([o.result for o in db.scalars(select(Operation))])
        for a in initial['accounts']:
            assert a['initial_password'] not in operations


def test_demo_refuses_an_unrelated_company():
    with Session(owner_engine) as db, db.begin():
        db.add(Organization(slug='demo', name='Existing real company'))
    with pytest.raises(ValueError, match='conflicts'):
        seed()
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(RateCard)) == 0
        assert db.get(User, DISPATCHER_ID) is None


def test_demo_is_atomic_when_profile_creation_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('simulated driver failure')
    monkeypatch.setattr('app.demo.save_driver', fail)
    with pytest.raises(ValueError, match='simulated driver failure'):
        seed()
    with Session(owner_engine) as db:
        for model in [Organization, RateCard, Shipper, Vehicle]:
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.get(User, DISPATCHER_ID) is None


def test_demo_keeps_an_existing_admin():
    # The conftest admin stands in for one created with app.bootstrap.
    result = seed()
    admin = next(a for a in result['accounts'] if a['role'] == 'ADMIN')
    assert admin == {'role': 'ADMIN', 'portal': 'platform', 'path': '/admin', 'login_id': 'owner', 'initial_password': None}
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'ADMIN')) == 1
        assert db.get(User, ADMIN_ID) is None
