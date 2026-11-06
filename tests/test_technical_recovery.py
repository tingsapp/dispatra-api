"""Real restricted PostgreSQL recovery/notification boundaries; no network/provider calls."""
from uuid import UUID
from unittest.mock import Mock
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.agents import technical_worker, diagnostics
from app.agents.models import TechnicalIssue
from app.events.models import Notification
from app.intake import mailbox
from app.intake.models import MailboxConnection
from app.models import now
from conftest import owner_engine
from test_email_intake import agent


def failure(code='CONNECTION_FAILED'):
    with Session(owner_engine) as db, db.begin():
        row = db.scalar(select(MailboxConnection))
        row.last_error, row.last_polled_at = code, now()
        row.version += 1
        return row.id


def reports():
    with Session(owner_engine) as db:
        return list(db.scalars(select(TechnicalIssue).where(TechnicalIssue.kind == 'MAILBOX_CONNECTION')))


def notices():
    with Session(owner_engine) as db:
        return db.scalar(select(func.count()).select_from(Notification).where(Notification.kind.like('technical.%')))


def test_verified_automatic_recovery_stays_quiet_and_never_fetches_mail(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    identity = failure()
    check, fetch = Mock(), Mock(side_effect=AssertionError('Automatic recovery cannot fetch messages'))
    monkeypatch.setattr(mailbox, 'check', check); monkeypatch.setattr(mailbox, 'fetch_new', fetch)
    technical_worker.cycle(force=True)
    row, = reports()
    assert row.status == 'FIXED' and row.automatic and row.fixed_count == 1 and row.diagnosis_source == 'RULES'
    assert check.call_count == 1 and fetch.call_count == 0 and notices() == 0
    with Session(owner_engine) as db: assert db.get(MailboxConnection, identity).last_error is None


def test_exhausted_recovery_escalates_once_and_does_not_loop(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    failure()
    check = Mock(side_effect=mailbox.MailboxError('CONNECTION_FAILED'))
    monkeypatch.setattr(mailbox, 'check', check)
    technical_worker.cycle(force=True); technical_worker.cycle(force=True)
    row, = reports()
    assert row.status == 'FAILED' and row.fixed_count == 0 and row.notified_at and row.diagnosis
    assert check.call_count == 1 and notices() == 1


def test_credential_failure_is_risky_and_never_probed_automatically(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    failure('AUTHENTICATION_FAILED')
    check = Mock(side_effect=AssertionError('Do not retry credentials automatically'))
    monkeypatch.setattr(mailbox, 'check', check)
    technical_worker.cycle(force=True)
    row, = reports()
    assert row.status == 'AWAITING_APPROVAL' and not row.automatic and row.diagnosis_source == 'RULES'
    assert check.call_count == 0 and notices() == 1


def test_settings_change_during_verification_cannot_clear_new_failure(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    identity = failure()
    def change_settings(*args):
        with Session(owner_engine) as db, db.begin():
            row = db.get(MailboxConnection, identity)
            row.version += 1; row.last_error = 'AUTHENTICATION_FAILED'
    monkeypatch.setattr(mailbox, 'check', change_settings)
    technical_worker.cycle(force=True)
    assert any(row.status == 'SUPERSEDED' and row.fixed_count == 0 for row in reports())
    with Session(owner_engine) as db: assert db.get(MailboxConnection, identity).last_error == 'AUTHENTICATION_FAILED'


def test_successful_browser_recovery_is_a_sample_not_an_admin_incident(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    with Session(owner_engine) as db: org = db.scalar(select(MailboxConnection.organization_id))
    diagnostics.record('WEB', 'READ_RECOVERED', org)
    technical_worker.cycle(force=True)
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(TechnicalIssue).where(TechnicalIssue.kind == 'RUNTIME_ERROR')) == 0
    assert notices() == 0


def test_flapping_exhausts_hourly_recovery_budget_and_requires_review(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    identity = failure()
    with Session(owner_engine) as db, db.begin():
        row = db.get(MailboxConnection, identity)
        for index in range(3):
            db.add(TechnicalIssue(organization_id=row.organization_id, fingerprint=f'previous:{index}', kind='MAILBOX_CONNECTION',
                title='Previous recovery', description='Previously verified.', status='FIXED', automatic=True, fixed_count=1,
                action='RETRY_MAILBOX', target_id=row.id, target_version=row.version - index - 1))
    check = Mock(side_effect=AssertionError('Hourly recovery limit exceeded'))
    monkeypatch.setattr(mailbox, 'check', check)
    technical_worker.cycle(force=True)
    assert any(row.status == 'AWAITING_APPROVAL' and not row.automatic for row in reports())
    assert check.call_count == 0 and notices() == 1


def test_successful_read_sample_cannot_hide_exhausted_or_api_failures(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    with Session(owner_engine) as db: org = db.scalar(select(MailboxConnection.organization_id))
    diagnostics.record('WEB', 'READ_RECOVERED', org)
    diagnostics.record('WEB', 'READ_RECOVERY_FAILED', org)
    diagnostics.record('API', 'HTTP_5XX', org)
    technical_worker.cycle(force=True); technical_worker.cycle(force=True)
    with Session(owner_engine) as db:
        rows = list(db.scalars(select(TechnicalIssue).where(TechnicalIssue.kind == 'RUNTIME_ERROR')))
        assert len(rows) == 2 and all(row.status == 'NEEDS_ADMIN' and row.diagnosis for row in rows)
    assert notices() == 2


def test_existing_alert_gets_an_assessment_without_repeat_notification(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    failure('AUTHENTICATION_FAILED')
    technical_worker.cycle(force=True)
    with Session(owner_engine) as db, db.begin():
        row = db.scalar(select(TechnicalIssue).where(TechnicalIssue.kind == 'MAILBOX_CONNECTION'))
        row.diagnosis = row.diagnosis_source = row.diagnosed_at = None
    technical_worker.cycle(force=True)
    row, = reports()
    assert row.diagnosis and row.diagnosis_source == 'RULES' and notices() == 1
