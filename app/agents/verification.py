"""Requeuing work is not a verified fix. Observe its actual terminal result before claiming success."""
from datetime import timedelta
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import now
from app.intake.models import EmailIntake, MailboxConnection
from .models import TechnicalIssue


def reconcile_mailboxes():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        issues = db.scalars(select(TechnicalIssue).join(MailboxConnection, and_(MailboxConnection.id == TechnicalIssue.target_id,
            MailboxConnection.organization_id == TechnicalIssue.organization_id)).where(TechnicalIssue.kind == 'MAILBOX_CONNECTION',
            TechnicalIssue.status.in_(['NEEDS_ADMIN', 'AWAITING_APPROVAL', 'FAILED']),
            or_(MailboxConnection.last_error.is_(None), MailboxConnection.enabled.is_(False),
                MailboxConnection.version != TechnicalIssue.target_version))
            .with_for_update(of=TechnicalIssue, skip_locked=True).limit(100))
        for issue in issues:
            row = db.scalar(select(MailboxConnection).where(MailboxConnection.id == issue.target_id,
                MailboxConnection.organization_id == issue.organization_id))
            if row is None: continue  # A missing target is not proof that its failure recovered.
            if row and row.enabled and row.version == issue.target_version and row.last_error is not None: continue
            issue.status = 'SUPERSEDED'
            issue.description = 'The connection recovered independently or its settings changed. This report no longer describes the current failure; no automatic fix is claimed.'
            issue.updated_at = now(); issue.version += 1


def verify_order_retries():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        issues = db.scalars(select(TechnicalIssue).where(TechnicalIssue.status.in_(['APPLYING', 'FAILED']),
            TechnicalIssue.action == 'RETRY_ORDER_AGENT').order_by((TechnicalIssue.status == 'APPLYING').desc(),
                TechnicalIssue.updated_at).with_for_update(skip_locked=True).limit(100))
        for issue in issues:
            row = db.scalar(select(EmailIntake).where(EmailIntake.id == issue.target_id,
                EmailIntake.organization_id == issue.organization_id))
            if row is None:
                status, message = 'SUPERSEDED', 'The saved processing task no longer exists; no successful repair is claimed.'
            elif row.version <= issue.target_version:
                status, message = 'FAILED', 'The approved retry was interrupted before requeuing. Review its outcome before another attempt.'
            elif row.status in ('ORDER_CREATED', 'NOT_AN_ORDER'):
                status, message = 'FIXED', 'Verified that the saved processing task reached a successful terminal result after retry.'
            elif row.status == 'DISCARDED':
                status, message = 'SUPERSEDED', 'The processing task was discarded; no successful repair is claimed.'
            elif row.status in ('FAILED', 'NEEDS_REVIEW', 'UNKNOWN_SENDER'):
                status, message = 'FAILED', 'The retried processing task still needs review. No further automatic replay will be attempted.'
            elif issue.updated_at < now() - timedelta(minutes=15):
                status, message = 'FAILED', 'The retry has not reached a verified result within 15 minutes. Check worker progress before replaying it.'
            else:
                continue
            if issue.status == status and issue.description == message: continue
            issue.status, issue.description, issue.updated_at = status, message, now()
            issue.fixed_count = int(status == 'FIXED')
            issue.version += 1
            if status == 'FAILED': issue.notified_at = None
