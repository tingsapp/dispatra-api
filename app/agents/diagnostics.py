"""Coarse platform error counters. Never collect messages, stacks, paths, tokens or payloads."""
from uuid import uuid4
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from .models import RuntimeDiagnostic


def record(source, kind, organization_id=None):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        seen = now()
        statement = insert(RuntimeDiagnostic).values(id=uuid4(), organization_id=organization_id, source=source, kind=kind,
            bucket=seen.replace(minute=0, second=0, microsecond=0), occurrences=1, last_seen_at=seen)
        db.execute(statement.on_conflict_do_update(constraint='runtime_diagnostic_window',
            set_={'occurrences': RuntimeDiagnostic.occurrences + 1, 'last_seen_at': seen}))


def api_failure(organization_id=None):
    try: record('API', 'HTTP_5XX', organization_id)
    except Exception:
        # A database outage must still return the original HTTP error.
        pass
