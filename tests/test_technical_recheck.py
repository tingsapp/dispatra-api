"""Recovery recheck: stale network results and distinct credential failure episodes."""
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.agents import repairs, technical_worker
from app.agents.models import TechnicalIssue
from app.intake import mailbox, service, vault
from app.intake.models import MailboxConnection, EmailIntake
from app.models import Organization, now
from conftest import owner_engine
from test_email_intake import agent, raw_email


def mailbox_snapshot():
    with Session(owner_engine) as db:
        row = db.scalar(select(MailboxConnection))
        return row.id, row.organization_id, row.version


def test_unreadable_password_starts_new_episode_after_prior_recovery(agent, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    identity, organization, version = mailbox_snapshot()
    with Session(owner_engine) as db, db.begin():
        db.add(TechnicalIssue(organization_id=organization, fingerprint=f'mailbox:{identity}:{version}',
            kind='MAILBOX_CONNECTION', title='Previous recovery', description='Verified previously.', status='FIXED',
            automatic=True, fixed_count=1, action='RETRY_MAILBOX', target_id=identity, target_version=version))
    monkeypatch.setattr(vault, 'unseal', Mock(side_effect=HTTPException(503, 'Cannot decode saved credentials')))
    assert service.poll_mailbox(organization) == 0
    assert service.poll_mailbox(organization) == 0
    with Session(owner_engine) as db:
        row = db.get(MailboxConnection, identity)
        assert row.version == version + 1 and row.last_error == 'PASSWORD_UNREADABLE'
    technical_worker.cycle(force=True)
    with Session(owner_engine) as db:
        report = db.scalar(select(TechnicalIssue).where(TechnicalIssue.fingerprint == f'mailbox:{identity}:{version + 1}'))
        assert report is not None and report.status == 'AWAITING_APPROVAL' and report.notified_at


def test_approved_retry_never_claims_success_after_settings_change(agent, monkeypatch):
    identity, organization, version = mailbox_snapshot()
    with Session(owner_engine) as db, db.begin(): db.get(MailboxConnection, identity).last_error = 'AUTHENTICATION_FAILED'
    def changed(*args, **kwargs):
        with Session(owner_engine) as db, db.begin():
            row = db.get(MailboxConnection, identity)
            row.version += 1; row.enabled = False; row.last_error = None
        return 0
    monkeypatch.setattr(service, 'poll_mailbox', changed)
    result, _ = repairs.apply(dict(organization_id=organization, target_id=identity, target_version=version, action='RETRY_MAILBOX'))
    assert result == 'SUPERSEDED'


@pytest.mark.parametrize('network_error', [False, True])
def test_poll_cannot_store_old_mail_or_error_after_settings_change(agent, monkeypatch, network_error):
    identity, organization, version = mailbox_snapshot()
    agent['inbox'].add(raw_email(body='Mail from the old settings'))
    original = mailbox.fetch_new
    def changed(*args):
        fetched = original(*args)
        with Session(owner_engine) as db, db.begin():
            row = db.get(MailboxConnection, identity)
            row.version += 1; row.host = 'new.example.test'; row.last_error = None
            row.uid_validity, row.last_uid = None, 0
        if network_error: raise mailbox.MailboxError('CONNECTION_FAILED')
        return fetched
    monkeypatch.setattr(mailbox, 'fetch_new', changed)
    assert service.poll_mailbox(organization) == 0
    with Session(owner_engine) as db:
        row = db.get(MailboxConnection, identity)
        assert row.version == version + 1 and row.last_error is None
        assert row.uid_validity is None and row.last_uid == 0
        assert db.scalar(select(func.count()).select_from(EmailIntake)) == 0


def test_approved_poll_does_not_contact_a_different_saved_version(agent, monkeypatch):
    identity, organization, version = mailbox_snapshot()
    fetch = Mock(side_effect=AssertionError('Stale approval cannot contact the mailbox'))
    monkeypatch.setattr(mailbox, 'fetch_new', fetch)
    assert service.poll_mailbox(organization, expected_target=(identity, version + 1)) == 0
    assert fetch.call_count == 0


def test_old_network_error_cannot_replace_a_newer_successful_poll(agent, monkeypatch):
    identity, organization, version = mailbox_snapshot()
    observed = []
    def newer_success(*args):
        with Session(owner_engine) as db, db.begin():
            row = db.get(MailboxConnection, identity)
            row.last_polled_at = now(); row.last_error = None
            observed.append(row.last_polled_at)
        raise mailbox.MailboxError('CONNECTION_FAILED')
    monkeypatch.setattr(mailbox, 'fetch_new', newer_success)
    assert service.poll_mailbox(organization) == 0
    with Session(owner_engine) as db:
        row = db.get(MailboxConnection, identity)
        assert row.version == version and row.last_error is None and row.last_polled_at == observed[0]


def test_poll_ignores_network_result_after_company_suspension(agent, monkeypatch):
    identity, organization, _ = mailbox_snapshot()
    agent['inbox'].add(raw_email(body='Arrives during suspension'))
    original = mailbox.fetch_new
    def suspended(*args):
        fetched = original(*args)
        with Session(owner_engine) as db, db.begin(): db.get(Organization, organization).active = False
        return fetched
    monkeypatch.setattr(mailbox, 'fetch_new', suspended)
    assert service.poll_mailbox(organization) == 0
    with Session(owner_engine) as db:
        assert db.scalar(select(func.count()).select_from(EmailIntake)) == 0


def test_approved_retry_rechecks_company_access_after_network_work(agent, monkeypatch):
    identity, organization, version = mailbox_snapshot()
    with Session(owner_engine) as db, db.begin(): db.get(MailboxConnection, identity).last_error = 'AUTHENTICATION_FAILED'
    def suspended(org, *, expected_target):
        assert org == organization and expected_target == (identity, version)
        with Session(owner_engine) as db, db.begin():
            db.get(Organization, organization).active = False
            db.get(MailboxConnection, identity).last_error = None
        return 0
    monkeypatch.setattr(service, 'poll_mailbox', suspended)
    result, _ = repairs.apply(dict(organization_id=organization, target_id=identity, target_version=version, action='RETRY_MAILBOX'))
    assert result == 'SUPERSEDED'
