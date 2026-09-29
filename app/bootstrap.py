"""Private platform-owner provisioning and recovery. Run interactively with migration credentials.

    python -m app.bootstrap                   create the single ADMIN
    python -m app.bootstrap --status          report whether an owner exists
    python -m app.bootstrap --reset-password  set a new owner password and revoke owner sessions

There is no public signup. Credentials are read from the terminal only and are never printed or stored in plaintext.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine, select, text, delete
from sqlalchemy.orm import Session
from .database import context
from .models import User, LoginSession, LoginBucket, now
# Register every mapped table so users' composite driver/customer foreign keys resolve in a standalone run.
from .operations import models as _operational_models  # noqa: F401
from .payments import models as _payment_models  # noqa: F401
from .schemas import LoginID, Password
from .security import hash_password, digest
from .services import audit

LOCK = 874012


def password_problem(login_id, password):
    try: TypeAdapter(Password).validate_python(password)
    except ValidationError: return 'Use 12 to 128 characters.'
    classes = sum(any(test(c) for c in password) for test in (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    if len(password) < 20 and classes < 3: return 'Use at least three of lowercase, uppercase, digits and symbols, or a passphrase of 20+ characters.'
    if len(set(password)) < 6: return 'Use a less repetitive password.'
    name = login_id.split('@')[0]
    if len(name) >= 3 and name in password.lower(): return 'Do not include the login ID in the password.'
    return None


def migration_problem(db):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(Config(str(Path(__file__).resolve().parent.parent / 'alembic.ini'))).get_current_head()
    try:
        with db.begin_nested(): current = db.scalar(text('SELECT version_num FROM alembic_version'))
    except Exception: return 'The database is not migrated. Run `alembic upgrade head` first.'
    return None if current == head else f'The database is at migration {current}; run `alembic upgrade head` ({head}) first.'


def owner(db):
    return db.scalar(select(User).where(User.role == 'ADMIN').with_for_update())


def create_owner(db, login_id, password):
    db.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': LOCK})
    context(db, platform=True)
    if owner(db): raise SystemExit('A platform owner already exists; nothing was changed. Use --reset-password to recover access.')
    account = User(scope='platform', login_id=login_id, role='ADMIN', password_hash=hash_password(password))
    db.add(account); db.flush()
    audit(db, account, 'platform_owner.created', account.id, None)
    return account


def reset_owner_password(db, password):
    db.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': LOCK})
    context(db, platform=True)
    account = owner(db)
    if not account: raise SystemExit('No platform owner exists. Run without --reset-password to create one.')
    account.password_hash = hash_password(password)
    account.active = True
    account.version += 1; account.updated_at = now()
    db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
    db.execute(delete(LoginBucket).where(LoginBucket.key == digest(f'login:None:{account.login_id}')))
    audit(db, account, 'platform_owner.password_reset', account.id, None)
    return account


def prompt_login():
    while True:
        try: return TypeAdapter(LoginID).validate_python(input('Platform owner login ID: ').strip().lower())
        except ValidationError: print('Use 3+ characters: lowercase letters, digits, and @ . _ + -', file=sys.stderr)


def prompt_password(login_id):
    while True:
        password = getpass.getpass('New platform owner password: ')
        problem = password_problem(login_id, password)
        if problem: print(problem, file=sys.stderr); continue
        if getpass.getpass('Confirm password: ') != password: print('Passwords do not match.', file=sys.stderr); continue
        return password


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.bootstrap', description='Provision or recover the Dispatra platform owner.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--status', action='store_true', help='report whether a platform owner exists')
    mode.add_argument('--reset-password', action='store_true', help='set a new owner password and revoke its sessions')
    args = parser.parse_args(argv)
    url = os.environ.get('MIGRATION_DATABASE_URL')
    if not url: raise SystemExit('Load the private .env first: MIGRATION_DATABASE_URL is not set.')
    engine = create_engine(url)
    with Session(engine) as db, db.begin():
        problem = migration_problem(db)
        if problem: raise SystemExit(problem)
        context(db, platform=True)
        existing = db.scalar(select(User).where(User.role == 'ADMIN'))
        login_id = existing.login_id if existing else None
        if args.status:
            print(f'Platform owner exists: {login_id} ({"active" if existing.active else "inactive"}).' if existing else 'No platform owner exists.')
            return
    if not sys.stdin.isatty(): raise SystemExit('Run this command in an interactive terminal; credentials are not accepted from pipes or files.')
    if args.reset_password:
        if not existing: raise SystemExit('No platform owner exists. Run without --reset-password to create one.')
        print(f'Resetting the password for platform owner {login_id}.')
        password = prompt_password(login_id)
        with Session(engine) as db, db.begin(): reset_owner_password(db, password)
        print('Password changed and existing owner sessions revoked. Sign in at /admin.')
        return
    if existing: raise SystemExit(f'A platform owner ({login_id}) already exists; nothing was changed. Use --reset-password to recover access.')
    login_id = prompt_login()
    password = prompt_password(login_id)
    with Session(engine) as db, db.begin(): create_owner(db, login_id, password)
    print(f'Platform owner {login_id} created. Sign in at /admin.')


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: raise SystemExit('\nCancelled; nothing was changed.')
