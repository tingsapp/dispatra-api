"""Deterministic authorization for the AI planner's only automatic connection tool."""
from datetime import timedelta
from sqlalchemy import func, select
from app.models import now
from .models import TechnicalIssue

TRANSIENT_MAILBOX = frozenset({'CONNECTION_FAILED', 'HOST_NOT_FOUND'})


def available(db, row):
    if not row.enabled or row.last_error not in TRANSIENT_MAILBOX: return False
    seen = db.scalar(select(TechnicalIssue.id).where(TechnicalIssue.target_id == row.id,
        TechnicalIssue.target_version == row.version).limit(1))
    if seen: return False
    attempts = db.scalar(select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.target_id == row.id,
        TechnicalIssue.automatic.is_(True), TechnicalIssue.created_at >= now() - timedelta(hours=1)))
    return attempts < 3
