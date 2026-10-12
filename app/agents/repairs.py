"""Execute only server-proposed, owner-approved repairs through bounded application use cases."""
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import Organization, now
from app.services import audit
from app.intake import service as intake
from app.intake.models import EmailIntake, MailboxConnection
from .models import TechnicalIssue


def claim():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.status == 'APPROVED').order_by(TechnicalIssue.updated_at)
                        .with_for_update(skip_locked=True).limit(1))
        if row is None: return None
        row.status, row.updated_at = 'APPLYING', now()
        row.version += 1
        return {key: getattr(row, key) for key in ('id', 'organization_id', 'action', 'target_id', 'target_version')}


def apply(task):
    org_id = task['organization_id']
    with Session(engine) as db, db.begin():
        context(db, org_id)
        org = db.get(Organization, org_id)
        if not org or not org.active: return 'SUPERSEDED', 'Company access changed; repair was not applied.'
        model = EmailIntake if task['action'] == 'RETRY_ORDER_AGENT' else MailboxConnection if task['action'] == 'RETRY_MAILBOX' else None
        if model is None: return 'FAILED', 'Unsupported repair; operator investigation required.'
        row = db.scalar(select(model).where(model.id == task['target_id'], model.organization_id == org_id).with_for_update())
        if row is None or row.version != task['target_version']: return 'SUPERSEDED', 'Repair target changed; a fresh report is required.'
        if task['action'] == 'RETRY_ORDER_AGENT':
            if row.status != 'FAILED': return 'SUPERSEDED', 'Processing state changed; retry is no longer applicable.'
            actor = SimpleNamespace(id=None, organization_id=org_id, actor_type='AGENT')
            intake.reopen(row)
            row.version += 1
            audit(db, actor, 'intake.retried', row.id, org_id, 'AGENT')
            return 'FIXED', 'Order agent task requeued through its normal processing workflow.'
        if not row.enabled or not row.last_error: return 'SUPERSEDED', 'Mailbox state changed; retry is no longer applicable.'
    # Network work remains outside the transaction; the shared mailbox poll deduplicates fetched mail.
    intake.poll_mailbox(org_id)
    with Session(engine) as db, db.begin():
        context(db, org_id)
        row = db.scalar(select(MailboxConnection).where(MailboxConnection.id == task['target_id'], MailboxConnection.organization_id == org_id))
        if row and row.last_error is None: return 'FIXED', 'Mailbox connection retry succeeded.'
    return 'FAILED', 'Mailbox retry failed; the dispatcher must review its settings.'


def execute(progress, limit=10):
    for _ in range(limit):
        task = claim()
        if task is None: break
        progress.step('Applying an admin-approved repair.', organization_id=task['organization_id'])
        try: status, message = apply(task)
        except Exception: status, message = 'FAILED', 'Approved repair failed; operator investigation required.'
        with Session(engine) as db, db.begin():
            context(db, platform=True)
            row = db.get(TechnicalIssue, task['id'])
            row.status, row.description, row.updated_at = status, message, now()
            row.fixed_count = int(status == 'FIXED')
            row.version += 1
            actor = SimpleNamespace(id=None, organization_id=None, actor_type='AGENT')
            audit(db, actor, 'platform_owner.technical_repair_finished', row.id, row.organization_id, 'AGENT')
        progress.step(message, organization_id=task['organization_id'], level='ERROR' if status == 'FAILED' else 'WARNING' if status == 'SUPERSEDED' else 'INFO')
