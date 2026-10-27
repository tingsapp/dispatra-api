"""Private owner-identity changes preserve credentials and tenant accounts."""
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app import bootstrap
from app.main import app
from app.models import User, LoginBucket, now
from app.security import digest
from conftest import HEADERS, PASSWORD, company, login, owner_engine

NEW = 'dispatra@tingsapp.com'


def test_change_owner_login_preserves_password_and_company_accounts(client):
    login(client)
    company(client)
    with TestClient(app, headers=HEADERS) as dispatcher:
        login(dispatcher, 'dispatch', 'acme', 'dispatcher')
        with Session(owner_engine) as db, db.begin():
            original = bootstrap.owner(db)
            identity, password, created = original.id, original.password_hash, original.created_at
            company_user = db.scalar(select(User).where(User.role == 'DISPATCHER'))
            company_before = (company_user.id, company_user.login_id, company_user.password_hash, company_user.version)
            keys = [digest(f'login:ADMIN:{value}') for value in ('owner', NEW)]
            unrelated = digest('login:ADMIN:unrelated')
            for key in [*keys, unrelated]:
                db.merge(LoginBucket(key=key, attempts=3, resets_at=now() + timedelta(minutes=5)))
        with Session(owner_engine) as db, db.begin():
            account = bootstrap.change_owner_login(db, ' Dispatra@Tingsapp.com ', 1)
            assert (account.id, account.login_id, account.password_hash, account.active, account.version, account.created_at) == (identity, NEW, password, True, 2, created)
            assert not db.scalars(select(LoginBucket).where(LoginBucket.key.in_(keys))).all()
            assert db.get(LoginBucket, unrelated) is not None
            other = db.scalar(select(User).where(User.role == 'DISPATCHER'))
            assert (other.id, other.login_id, other.password_hash, other.version) == company_before
        assert client.get('/api/v1/auth/me').status_code == 401
        assert dispatcher.get('/api/v1/auth/me').status_code == 200
        assert client.post('/api/v1/auth/login', json={'portal': 'platform', 'login_id': 'owner', 'password': PASSWORD}).status_code == 401
        assert login(client, login_id=NEW)['login_id'] == NEW
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users WHERE role='ADMIN'")) == 1
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='platform_owner.login_changed'")) == 1


def test_change_owner_login_noop_preserves_session_and_version(client):
    login(client)
    with Session(owner_engine) as db, db.begin():
        assert bootstrap.change_owner_login(db, 'owner', 1).version == 1
    assert client.get('/api/v1/auth/me').status_code == 200
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='platform_owner.login_changed'")) == 0


def test_change_owner_login_rejects_stale_version_and_invalid_identifier():
    with pytest.raises(SystemExit, match='Owner account changed'):
        with Session(owner_engine) as db, db.begin(): bootstrap.change_owner_login(db, NEW, 2)
    with pytest.raises(ValidationError):
        with Session(owner_engine) as db, db.begin(): bootstrap.change_owner_login(db, '<invalid>', 1)
    with Session(owner_engine) as db, db.begin():
        a = bootstrap.owner(db)
        assert a.login_id == 'owner' and a.version == 1


def test_change_owner_login_preserves_disabled_owner_and_refuses_missing_owner():
    with Session(owner_engine) as db, db.begin():
        a = bootstrap.owner(db)
        a.active = False
    with Session(owner_engine) as db, db.begin():
        assert bootstrap.change_owner_login(db, NEW, 1).active is False
    with owner_engine.begin() as db: db.execute(text('TRUNCATE users CASCADE'))
    with pytest.raises(SystemExit, match='No platform owner'):
        with Session(owner_engine) as db, db.begin(): bootstrap.change_owner_login(db, NEW, 1)


def test_change_owner_login_cli_refuses_noninteractive_input():
    result = subprocess.run([sys.executable, '-m', 'app.bootstrap', '--change-login'], cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'MIGRATION_DATABASE_URL': os.environ['TEST_OWNER_DATABASE_URL']},
                            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    assert result.returncode != 0 and 'interactive terminal' in result.stderr
    with Session(owner_engine) as db, db.begin(): assert bootstrap.owner(db).login_id == 'owner'
