"""Destructive private reset acceptance runs only on disposable PostgreSQL."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.main import app
from app.database import engine
from app.models import User
from app.company_billing.models import CompanyBilling
from app.maintenance_reset import reset_application_data, tables, counts, PRESERVED
from conftest import HEADERS, owner_engine, owner_url, login, company
from test_manual_operations import setup, new_order, assign, finish


def args(db, **updates):
    values = dict(database=owner_url.rsplit('/', 1)[-1], keep_owner='owner', owner_version=1,
                  expected_tables=tables(db), operation_id=uuid4())
    values.update(updates)
    return values


def test_reset_clears_business_history_keeps_owner_config_and_retry_preserves_new_company(setup):
    w = setup
    finish(w, assign(w, new_order(w)))
    with TestClient(app, headers=HEADERS) as admin:
        login(admin)
        with Session(owner_engine) as db, db.begin():
            before_owner = dict(db.execute(select(User.__table__).where(User.role == 'ADMIN')).mappings().one())
            options = args(db)
            result = reset_application_data(db, **options)
            after = counts(db, tables(db))
            assert after['users'] == 1 and after['audit_events'] == after['operations'] == after['outbox_events'] == 1
            assert all(after[name] == 0 for name in after if name not in PRESERVED | {'users', 'audit_events', 'operations', 'outbox_events'})
            assert dict(db.execute(select(User.__table__)).mappings().one()) == before_owner
            assert result['removed_counts']['orders'] == result['removed_counts']['delivery_evidence'] == 1
            assert result['owner_identity_and_password_preserved']
        assert admin.get('/api/v1/auth/me').status_code == 401
        assert w['client'].get('/api/v1/auth/me').status_code == 401
        assert w['mobile'].get('/api/v1/auth/me').status_code == 401
        login(admin)
        fresh = company(admin, 'fresh')
        with Session(owner_engine) as db, db.begin():
            replay = reset_application_data(db, **options)
            assert replay == result
            assert db.scalar(text('SELECT count(*) FROM organizations')) == 1
            assert db.scalar(text('SELECT count(*) FROM company_settings')) == 1
            assert db.scalar(text('SELECT count(*) FROM rate_cards')) > 0
            assert db.scalar(text('SELECT count(*) FROM catalog_entries')) > 0
            assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='platform_owner.data_reset'")) == 1
        assert fresh['slug'] == 'fresh'


@pytest.mark.parametrize('changes', [dict(database='wrong_test'), dict(keep_owner='other@example.com'), dict(owner_version=99), dict(expected_tables=['users'])])
def test_reset_rejects_unapproved_target_and_schema_before_writes(client, changes):
    login(client); company(client)
    with Session(owner_engine) as db, db.begin():
        before = counts(db, tables(db))
        options = args(db, **changes)
    with pytest.raises(ValueError):
        with Session(owner_engine) as db, db.begin(): reset_application_data(db, **options)
    with Session(owner_engine) as db:
        assert counts(db, tables(db)) == before


def test_reset_refuses_external_subscriptions(client):
    login(client); org = company(client)
    with Session(owner_engine) as db, db.begin():
        db.add(CompanyBilling(organization_id=org['id'], stripe_subscription='sub_disposable_test'))
    with pytest.raises(ValueError, match='External subscriptions'):
        with Session(owner_engine) as db, db.begin(): reset_application_data(db, **args(db))
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM organizations')) == 1
        assert db.scalar(text('SELECT count(*) FROM company_billing')) == 1


def test_reset_is_unavailable_to_restricted_runtime_role(client):
    login(client); company(client)
    with Session(owner_engine) as db: options = args(db)
    with pytest.raises(PermissionError, match='private migration'):
        with Session(engine) as db, db.begin(): reset_application_data(db, **options)
    with owner_engine.connect() as db: assert db.scalar(text('SELECT count(*) FROM organizations')) == 1


def test_reset_transaction_rolls_back_all_data_and_owner_credentials(setup):
    new_order(setup)
    with Session(owner_engine) as db: before = counts(db, tables(db))
    with pytest.raises(RuntimeError, match='injected failure'):
        with Session(owner_engine) as db, db.begin():
            reset_application_data(db, **args(db))
            raise RuntimeError('injected failure before commit')
    with Session(owner_engine) as db: assert counts(db, tables(db)) == before
    assert setup['client'].get('/api/v1/auth/me').status_code == 200
    assert setup['mobile'].get('/api/v1/auth/me').status_code == 200
