"""The contact migration preserves old accounts and fails without rewriting duplicates."""
import importlib.util
import json
from pathlib import Path
from uuid import uuid4
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from conftest import owner_engine


@pytest.mark.parametrize('duplicate', ['email', 'phone'])
def test_duplicate_contacts_stop_migration_and_leave_accounts_unchanged(duplicate):
    path = Path(__file__).parents[1] / 'migrations/versions/0038_role_login.py'
    spec = importlib.util.spec_from_file_location('role_login_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # PostgreSQL transactional DDL restores the current schema at the end.
    with owner_engine.connect() as db:
        transaction = db.begin()
        try:
            with Operations.context(MigrationContext.configure(db)):
                migration.downgrade()
                identities = []
                for index in range(2):
                    org, user = uuid4(), uuid4()
                    identities.append(user)
                    db.execute(text("""INSERT INTO organizations
                        (id, slug, name, active, version, created_at, updated_at)
                        VALUES (:id, :slug, 'Existing company', true, 1, now(), now())"""),
                        {'id': org, 'slug': f'existing-{index}'})
                    db.execute(text("""INSERT INTO users
                        (id, organization_id, scope, login_id, password_hash, role, active,
                         display_name, version, created_at, updated_at)
                        VALUES (:id, :org, :scope, :login, 'existing-hash', 'DISPATCHER', true,
                                'Existing user', 3, now(), now())"""),
                        {'id': user, 'org': org, 'scope': str(org),
                         'login': 'same@example.com' if duplicate == 'email' else f'user-{index}@example.com'})
                    db.execute(text("""INSERT INTO company_settings
                        (id, organization_id, data, version, created_at, updated_at)
                        VALUES (:id, :org, cast(:data AS jsonb), 1, now(), now())"""),
                        {'id': uuid4(), 'org': org, 'data': json.dumps({'phone':
                            '6045550100' if not index else '+1 (604) 555-0100' if duplicate == 'phone' else '6045550101'})})
                before = db.execute(text('SELECT id, login_id, password_hash, version FROM users ORDER BY id')).all()
                savepoint = db.begin_nested()
                with pytest.raises(DBAPIError, match='Resolve duplicate email/phone accounts'):
                    migration.upgrade()
                savepoint.rollback()
                assert db.execute(text('SELECT id, login_id, password_hash, version FROM users ORDER BY id')).all() == before
                assert not db.scalar(text("SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='phone'"))
                # Explicitly resolving the conflicting identity allows migration;
                # all remaining identities, hashes and versions stay intact.
                db.execute(text('UPDATE users SET login_id=:login WHERE id=:id'), {'login': 'resolved@example.com', 'id': identities[1]})
                db.execute(text("UPDATE company_settings SET data='{}'::jsonb WHERE organization_id=(SELECT organization_id FROM users WHERE id=:id)"), {'id': identities[1]})
                before = db.execute(text('SELECT id, login_id, password_hash, version FROM users ORDER BY id')).all()
                migration.upgrade()
                assert db.execute(text('SELECT id, login_id, password_hash, version FROM users ORDER BY id')).all() == before
                assert db.scalar(text('SELECT phone FROM users WHERE id=:id'), {'id': identities[0]}) == '6045550100'
        finally:
            transaction.rollback()
