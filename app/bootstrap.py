"""Run once with migration credentials; never exposes public account signup."""
import getpass
import os
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from .models import User
from .schemas import LoginID, Password
from pydantic import TypeAdapter
from .security import hash_password
from .services import audit

if __name__ == '__main__':
    login = TypeAdapter(LoginID).validate_python(input('Platform owner login ID: ').strip().lower())
    password = TypeAdapter(Password).validate_python(getpass.getpass('Platform owner password (12+ characters): '))
    with Session(create_engine(os.environ['MIGRATION_DATABASE_URL'])) as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(874012)"))
        if db.scalar(select(User).where(User.role == 'PLATFORM_OWNER')):
            raise SystemExit('A platform owner already exists. Bootstrap did not change it.')
        owner = User(scope='platform', login_id=login, role='PLATFORM_OWNER', password_hash=hash_password(password))
        db.add(owner)
        db.flush()
        from .database import context
        context(db, platform=True)
        audit(db, owner, 'platform_owner.created', owner.id, None)
    print('Platform owner created. Sign in at /platform.')
