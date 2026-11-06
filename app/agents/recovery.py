"""Bounded, version-checked automatic repair. No mail, orders or provider writes are replayed."""
from datetime import timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import Organization, now
from app.intake import mailbox, vault
from app.intake.models import MailboxConnection
from app.services import audit
from .brain import save_assessment
from .checks import SYSTEM
from .models import TechnicalIssue
from .recovery_policy import TRANSIENT_MAILBOX, available


def _claim(identity):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        row = db.scalar(select(MailboxConnection).join(Organization, Organization.id == MailboxConnection.organization_id)
            .where(MailboxConnection.id == identity, Organization.active.is_(True), MailboxConnection.enabled.is_(True))
            .with_for_update(of=MailboxConnection))
        if row is None or not available(db, row): return None
        fingerprint = f'mailbox:{row.id}:{row.version}'
        issue = TechnicalIssue(organization_id=row.organization_id, fingerprint=fingerprint, kind='MAILBOX_CONNECTION',
            title='Mailbox connection recovery', description='Verifying a transient connection failure using a read-only probe.',
            status='APPLYING', automatic=True, action='RETRY_MAILBOX', target_id=row.id, target_version=row.version)
        db.add(issue); db.flush()
        # Only the private executor sees connection settings. None enter AI inputs, logs or reports.
        return dict(id=issue.id, target=row.id, organization=row.organization_id, version=row.version,
            observed=row.last_polled_at, error=row.last_error,
            settings=(row.host, row.port, row.username, row.secret, row.folder))


def _probe(task):
    host, port, username, secret, folder = task['settings']
    try:
        password = vault.unseal(secret)
        mailbox.check(host, port, username, password, folder)
        return True
    except Exception:
        # Unknown provider/decryption errors are not a reason to relax checks or expose raw messages.
        return False


def _finish(task, plan, source, verified):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        issue = db.scalar(select(TechnicalIssue).where(TechnicalIssue.id == task['id']).with_for_update())
        row = db.scalar(select(MailboxConnection).where(MailboxConnection.id == task['target'],
            MailboxConnection.organization_id == task['organization']).with_for_update())
        active = db.scalar(select(Organization.active).where(Organization.id == task['organization']))
        if issue is None or issue.status != 'APPLYING': return
        save_assessment(issue, plan, source, now())
        issue.updated_at = now(); issue.version += 1
        if not active or row is None or not row.enabled or row.version != task['version']:
            issue.status, issue.description = 'SUPERSEDED', 'Settings or company access changed; no recovery result was applied.'
        elif row.last_error is None:
            issue.status, issue.description = 'SUPERSEDED', 'The normal mailbox worker recovered independently; no automatic fix is claimed.'
        elif row.last_polled_at != task['observed'] or row.last_error != task['error']:
            issue.status, issue.description = 'FAILED', 'Newer mailbox evidence arrived during verification. Review its current failure; no settings or messages were changed.'
        elif plan.recommendation != 'CHECK_MAILBOX':
            issue.status, issue.description = 'NEEDS_ADMIN', 'Automatic recovery was withheld because the diagnosis requires review. No mailbox changes were made.'
        elif verified:
            row.last_error = None
            issue.status, issue.fixed_count = 'FIXED', 1
            issue.description = 'Verified the saved mailbox connection and folder, then cleared the matching connection error. No messages were fetched, sent or reprocessed.'
            audit(db, SYSTEM, 'platform_owner.mailbox_connection_recovered', row.id, row.organization_id, 'AGENT')
        else:
            issue.status, issue.description = 'FAILED', 'Read-only mailbox verification failed. Automatic retry is exhausted for this failure; review the connection, credentials and provider availability.'


def recover(progress, brain, limit=5):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        # Exclude attempted episodes in SQL so persistent failures cannot starve new incidents.
        seen = select(TechnicalIssue.id).where(TechnicalIssue.target_id == MailboxConnection.id,
            TechnicalIssue.target_version == MailboxConnection.version).exists()
        attempts = select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.target_id == MailboxConnection.id,
            TechnicalIssue.automatic.is_(True), TechnicalIssue.created_at >= now() - timedelta(hours=1)).scalar_subquery()
        identities = list(db.scalars(select(MailboxConnection.id).join(Organization, Organization.id == MailboxConnection.organization_id)
            .where(Organization.active.is_(True), MailboxConnection.enabled.is_(True),
                MailboxConnection.last_error.in_(TRANSIENT_MAILBOX), ~seen, attempts < 3)
            .order_by(MailboxConnection.last_polled_at).limit(limit)))
    for identity in identities:
        task = _claim(identity)
        if task is None: continue
        progress.step('Diagnosing a transient mailbox connection failure.', organization_id=task['organization'])
        plan, source = brain.assess('MAILBOX_CONNECTION', {'error_code': task['error'], 'attempt_limit': 1}, safe_mailbox_check=True)
        progress.step('Verifying the safe recovery plan.', organization_id=task['organization'])
        _finish(task, plan, source, _probe(task) if plan.recommendation == 'CHECK_MAILBOX' else False)
