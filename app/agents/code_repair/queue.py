"""Hold notifications while local code repair is possible; escalate only terminal failures."""
from datetime import timedelta
import os
from pathlib import Path
from uuid import UUID
from sqlalchemy import select
from app.models import now
from ..models import TechnicalIssue, RuntimeDiagnostic


def enabled():
    return os.environ.get('TECHNICAL_LOCAL_CODE_REPAIR') == '1' or os.environ.get('TECHNICAL_PRODUCTION_CODE_REPAIR') == '1'


def roots():
    api = Path(__file__).resolve().parents[3]
    return {'api': api, 'client': api.parent / 'client'}


def diagnostic(db, issue):
    try: identity = UUID(issue.fingerprint.removeprefix('runtime:').split(':', 1)[0])
    except ValueError: return None
    return db.get(RuntimeDiagnostic, identity)


def enqueue(db):
    if not enabled(): return
    for row in db.scalars(select(TechnicalIssue).where(TechnicalIssue.kind == 'RUNTIME_ERROR',
            TechnicalIssue.status == 'NEEDS_ADMIN', TechnicalIssue.repair_stage.is_(None),
            TechnicalIssue.notified_at.is_(None)).order_by(TechnicalIssue.created_at).limit(20)).all():
        evidence = diagnostic(db, row)
        if evidence is None or evidence.source not in ('API', 'WEB'): continue
        row.status, row.repair_stage = 'APPLYING', 'QUEUED'
        row.repair_summary = 'Waiting for isolated diagnosis and tests. No live code has changed.'
        row.automatic = True; row.version += 1; row.updated_at = now()
    # The separate runner may be down. Never leave a queued incident silently stuck forever.
    for row in db.scalars(select(TechnicalIssue).where(TechnicalIssue.status == 'APPLYING',
            TechnicalIssue.repair_stage.is_not(None), TechnicalIssue.updated_at < now() - timedelta(minutes=45))).all():
        row.status, row.repair_stage = 'FAILED', 'ESCALATED'
        row.repair_summary = 'The code-repair worker stopped reporting progress. Verify its outcome before retrying.'
        row.notified_at = None; row.updated_at = now(); row.version += 1
