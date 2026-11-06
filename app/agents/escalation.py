"""Assess unresolved/risky incidents and notify once; successful recoveries stay quiet."""
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from app.events import publish
from .brain import save_assessment
from .checks import SYSTEM
from .models import TechnicalIssue

ATTENTION = ('NEEDS_ADMIN', 'AWAITING_APPROVAL', 'FAILED')


def escalate(progress, brain, limit=100):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        pending = list(db.scalars(select(TechnicalIssue.id).where(TechnicalIssue.status.in_(ATTENTION),
            or_(TechnicalIssue.notified_at.is_(None), TechnicalIssue.diagnosed_at.is_(None)))
            .order_by(TechnicalIssue.created_at).limit(limit)))
    for identity in pending:
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.get(TechnicalIssue, identity)
            if row is None or row.status not in ATTENTION or (row.notified_at and row.diagnosed_at): continue
            version, kind, evidence = row.version, row.kind, row.description
        progress.step('Assessing an unresolved or risky problem before escalation.')
        plan, source = brain.assess(kind, evidence)
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.id == identity).with_for_update())
            if row is None or row.version != version or row.status not in ATTENTION or (row.notified_at and row.diagnosed_at): continue
            save_assessment(row, plan, source, now())
            row.updated_at = now()
            if row.notified_at is None:
                row.notified_at = now()
                publish.record(db, SYSTEM, 'technical.approval_required' if row.status == 'AWAITING_APPROVAL'
                    else 'technical.problem_detected', row.id, row.organization_id, 'AGENT')
