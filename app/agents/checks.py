from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from app.models import Organization, OutboxEvent, now
from app.integrations.models import ApiWebhook, TmsConnection, TmsInbox
from app.intake.models import EmailIntake, MailboxConnection
from app.operations.models import EmailDelivery, Order, Settings
from app.events import publish
from .models import AgentRun, RuntimeDiagnostic, TechnicalIssue
from .maintenance import clean_expired_sessions

SYSTEM = SimpleNamespace(id=None, role='TECHNICAL', organization_id=None, actor_type='AGENT')


def report(db, progress, fingerprint, kind, title, description, organization_id=None, action=None, target=None, severity='CRITICAL'):
    identity = db.scalar(insert(TechnicalIssue).values(id=uuid4(), organization_id=organization_id, fingerprint=fingerprint,
        kind=kind, severity=severity, title=title, description=description, status='AWAITING_APPROVAL' if action else 'NEEDS_ADMIN',
        action=action, target_id=target.id if target else None, target_version=target.version if target else None,
        approved_by=None, automatic=False, fixed_count=0, version=1, created_at=now(), updated_at=now())
        .on_conflict_do_nothing(index_elements=['fingerprint']).returning(TechnicalIssue.id))
    if identity:
        progress.step(title, organization_id=organization_id, level='WARNING')
        publish.record(db, SYSTEM, 'technical.approval_required' if action else 'technical.problem_detected', identity, organization_id, 'AGENT')


def require_runtime_role(db):
    unsafe = db.scalar(text("SELECT rolsuper OR rolbypassrls OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND pg_has_role(current_user, tableowner, 'USAGE')) FROM pg_roles WHERE rolname = current_user"))
    if unsafe: raise RuntimeError('Restricted runtime credentials required.')


def scan(db, progress):
    require_runtime_role(db)
    progress.step('Database connection and restricted runtime role verified.', 15)
    count = clean_expired_sessions(db, SYSTEM, progress.id)
    if count:
        db.add(TechnicalIssue(fingerprint=f'expired-sessions:{progress.id}', kind='EXPIRED_SESSIONS', severity='WARNING',
            title='Expired sessions cleaned', description=f'Removed {count} already-expired session(s).', status='FIXED', automatic=True, fixed_count=count))
        progress.step(f'Automatically cleaned {count} expired session(s).', 25)
    else: progress.step('Session maintenance checked; no expired sessions.', 25)
    progress.step('Checking Order agent processing and mailbox connections.', 35)
    for row in db.scalars(select(EmailIntake).join(Organization, Organization.id == EmailIntake.organization_id)
                          .where(Organization.active.is_(True), EmailIntake.status == 'FAILED')):
        report(db, progress, f'intake:{row.id}:{row.version}', 'ORDER_PROCESSING', 'Order agent processing failed',
            'Reprocess the saved email through normal booking checks. This may use the AI provider and needs admin approval.',
            row.organization_id, 'RETRY_ORDER_AGENT', row)
    for row in db.scalars(select(MailboxConnection).join(Organization, Organization.id == MailboxConnection.organization_id)
                          .where(Organization.active.is_(True), MailboxConnection.enabled.is_(True), MailboxConnection.last_error.is_not(None))):
        report(db, progress, f'mailbox:{row.id}:{row.version}', 'MAILBOX_CONNECTION', 'Order mailbox connection failed',
            'Retry the connection using saved company settings. The dispatcher may need to update its credentials.',
            row.organization_id, 'RETRY_MAILBOX', row)
    progress.step('Checking delivery uncertainty and application errors.', 60)
    for model, kind, title, predicate in [
        (TmsConnection, 'TMS_CONNECTION', 'TMS connection needs investigation', (TmsConnection.last_error.is_not(None) | TmsConnection.last_status_error.is_not(None)) & TmsConnection.enabled.is_(True)),
        (ApiWebhook, 'WEBHOOK_DELIVERY', 'Company webhook delivery failed', ApiWebhook.last_error.is_not(None) & ApiWebhook.enabled.is_(True)),
        (TmsInbox, 'TMS_PROCESSING', 'TMS order processing needs review', TmsInbox.status.in_(['FAILED', 'NEEDS_REVIEW'])),
    ]:
        for row in db.scalars(select(model).join(Organization, Organization.id == model.organization_id).where(Organization.active.is_(True), predicate)):
            report(db, progress, f'{kind}:{row.id}:{row.version}', kind, title,
                'The dispatcher must review the saved integration settings or processing result. External delivery is not replayed automatically.', row.organization_id)
    for row in db.scalars(select(OutboxEvent).where(OutboxEvent.last_error_code.is_not(None), OutboxEvent.attempts >= 3)):
        report(db, progress, f'outbox:{row.id}', 'BACKGROUND_DELIVERY', 'Background delivery has repeated failures',
            'The delivery worker has recorded repeated failures. Operator investigation is required; no extra delivery is sent by the Technical agent.', row.organization_id)
    for row in db.scalars(select(EmailDelivery).where(EmailDelivery.status == 'UNKNOWN')):
        report(db, progress, f'email:{row.id}:{row.version}', 'EMAIL_UNCERTAIN', 'Email delivery needs investigation',
            'Delivery may already have succeeded. The company dispatcher must confirm the outcome before any resend.', row.organization_id)
    for row in db.scalars(select(RuntimeDiagnostic).where(RuntimeDiagnostic.last_seen_at >= now() - timedelta(hours=1))):
        report(db, progress, f'runtime:{row.id}', 'RUNTIME_ERROR', f'{row.source.title()} application error reported',
            f'{row.occurrences} {row.kind.lower().replace("_", " ")} report(s). Technical investigation is required; code and deployment changes require review.', row.organization_id)
    progress.step('Checking worker heartbeats and queued work.', 80)
    pending_order = db.scalar(select(func.count()).select_from(EmailIntake).join(Organization, Organization.id == EmailIntake.organization_id)
        .where(EmailIntake.status == 'RECEIVED', Organization.active.is_(True)))
    pending_dispatch = db.scalar(select(func.count()).select_from(Order).join(Settings, Settings.organization_id == Order.organization_id)
        .join(Organization, Organization.id == Order.organization_id).where(Organization.active.is_(True), Order.status == 'NEW', Settings.data['dispatch_mode'].astext == 'AUTO'))
    pending_operational = db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.agent == 'OPERATIONAL', AgentRun.status == 'QUEUED'))
    for agent, pending in [('ORDER', pending_order), ('DISPATCH', pending_dispatch), ('OPERATIONAL', pending_operational)]:
        latest = db.scalar(select(AgentRun).where(AgentRun.agent == agent).order_by(AgentRun.created_at.desc()).limit(1))
        if (pending or latest and latest.status == 'RUNNING') and (not latest or latest.heartbeat_at < now() - timedelta(minutes=5)):
            report(db, progress, f'worker:{agent}:{latest.id if latest else "not-started"}', 'WORKER_PROGRESS', f'{agent.title()} agent progress is overdue',
                'The agent has no recent progress heartbeat. Service availability needs operator investigation.')
    progress.step('Platform checks completed; repairs requiring approval are reported.', 95)
