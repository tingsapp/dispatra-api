import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from fastapi import HTTPException

# Runtime credentials must never be the migration/owner credentials.
engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+psycopg://dispatra_app@localhost:5432/dispatra"), pool_pre_ping=True)

def context(db: Session, organization_id=None, platform=False, customer_id=None):
    db.execute(text("SELECT set_config('app.organization_id', :org, true), set_config('app.platform', :platform, true), set_config('app.customer_id', :customer, true)"),
               {"org": str(organization_id or ''), "platform": 'true' if platform else 'false', "customer": str(customer_id or '')})

def session():
    with Session(engine, expire_on_commit=False) as db:
        with db.begin():
            unsafe = db.scalar(text("SELECT rolsuper OR rolbypassrls OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND pg_has_role(current_user, tableowner, 'USAGE')) FROM pg_roles WHERE rolname = current_user"))
            if unsafe: raise HTTPException(503, 'Runtime database credentials must use a restricted non-owner role.')
            context(db)
            yield db
