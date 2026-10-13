"""Owner-only progress, diagnostics and bounded repair authorization on restricted PostgreSQL."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.main import app
from app.database import context, engine
from app.models import LoginSession, User, now
from app.agents import technical_worker, diagnostics
from app.agents.models import AgentRun, AgentLog, TechnicalIssue, RuntimeDiagnostic
from app.agents.telemetry import monitor_run
from app.intake import extract, worker as order_worker
from app.intake.models import EmailIntake
from conftest import HEADERS, login, company, post, owner_engine
from test_email_intake import agent, raw_email
from test_manual_operations import setup

ROOT = '/api/v1/platform/agents'


def snapshot(client):
    response = client.get(ROOT + '/monitor')
    assert response.status_code == 200, response.text
    return response.json()


def failed_intake(agent):
    agent['extracted']['value'] = extract.Unavailable('AI_UNAVAILABLE', retry=False)
    agent['inbox'].add(raw_email(body='Private customer content'))
    order_worker.cycle()
    with Session(owner_engine) as db:
        return db.scalar(select(EmailIntake.id).where(EmailIntake.status == 'FAILED'))


def test_owner_monitor_auth_rls_and_live_progress(client):
    login(client); org = company(client)
    initial = snapshot(client)
    assert len(initial['agents']) == 4 and all(item['run'] is None for item in initial['agents'])
    with monitor_run('ORDER') as progress:
        progress.step('Validating queued work.', 40, UUID(org['id']))
        live = snapshot(client)
        assert live['running'] == 1
        assert live['agents'][0]['run']['progress'] == 40
    assert snapshot(client)['agents'][0]['run']['status'] == 'SUCCEEDED'
    page = client.get(ROOT + '/logs', params={'agent': 'ORDER', 'limit': 1}).json()
    assert len(page['items']) == 1 and page['next_before']
    assert client.get(ROOT + '/logs', params={'agent': 'DISPATCH', 'before': page['next_before']}).status_code == 404
    assert client.get(ROOT + '/logs', params={'agent': 'ORDER', 'before': page['next_before']}).json()['items'][0]['company_name'] == 'Acme'
    with TestClient(app, headers=HEADERS) as anonymous:
        assert anonymous.get(ROOT + '/monitor').status_code == 401
    login(client, 'dispatch', 'acme', 'dispatcher')
    for path in ('/monitor', '/logs', '/issues'):
        assert client.get(ROOT + path).status_code == 403
    assert post(client, ROOT + '/technical/check', {}).status_code == 403
    with Session(engine) as db, db.begin():
        context(db, org['id'])
        assert db.scalar(select(func.count()).select_from(AgentLog)) == 0
    with pytest.raises(SQLAlchemyError), Session(engine) as db, db.begin():
        context(db, org['id'])
        db.add(AgentRun(agent='TECHNICAL', step='Intruder'))
        db.flush()


def test_automatic_expired_session_cleanup_preserves_active_sessions_and_coalesces_checks(client):
    owner = login(client); company(client)
    with Session(owner_engine) as db, db.begin():
        db.add(LoginSession(token_hash='0' * 64, user_id=UUID(owner['id']), expires_at=now() - timedelta(days=1)))
    key = str(uuid4())
    check = post(client, ROOT + '/technical/check', {}, key)
    assert check.status_code == 202
    assert post(client, ROOT + '/technical/check', {}, key).json() == check.json()
    assert post(client, ROOT + '/technical/check', {}).json()['id'] == check.json()['id']
    assert technical_worker.cycle(force=True)
    result = snapshot(client)
    assert result['automatic_fixes_24h'] == 1 and client.get('/api/v1/auth/me').status_code == 200
    with Session(owner_engine) as db:
        assert db.get(LoginSession, '0' * 64) is None
    assert client.get(ROOT + '/issues', params={'status': 'ALL'}).json()['items'][0]['automatic'] is True


def test_failed_order_processing_needs_versioned_owner_approval_and_requeues_once(agent):
    identity = failed_intake(agent)
    assert identity
    technical_worker.cycle(force=True)
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        report, = [row for row in snapshot(owner)['issues'] if row['kind'] == 'ORDER_PROCESSING']
        assert report['status'] == 'AWAITING_APPROVAL' and 'target_id' not in report
        assert 'Private customer content' not in str(snapshot(owner))
        with Session(owner_engine) as db: assert db.get(EmailIntake, identity).status == 'FAILED'
        assert post(agent['client'], ROOT + f"/issues/{report['id']}/approve", {'version': 1}).status_code == 403
        assert post(owner, ROOT + f"/issues/{report['id']}/approve", {'version': 99}).status_code == 409
        key = str(uuid4())
        path = ROOT + f"/issues/{report['id']}/approve"
        approved = post(owner, path, {'version': 1}, key)
        assert approved.status_code == 202 and approved.json()['status'] == 'APPROVED'
        # Persisted pre-flowchart responses must remain replayable after adding agent attribution.
        from app.models import Operation
        with Session(owner_engine) as db, db.begin():
            operation = db.scalar(select(Operation).where(Operation.key.endswith(':' + key)))
            operation.result = {field: value for field, value in operation.result.items() if field != 'agent'}
        assert post(owner, path, {'version': 1}, key).json() == approved.json()
        technical_worker.cycle(force=True)
        with Session(owner_engine) as db:
            row = db.get(EmailIntake, identity)
            assert row.status == 'RECEIVED' and row.attempts == 0
            version = row.version
            assert db.get(TechnicalIssue, UUID(report['id'])).status == 'FIXED'
        technical_worker.cycle(force=True)
        with Session(owner_engine) as db: assert db.get(EmailIntake, identity).version == version
        notices = owner.get('/api/v1/platform/notifications').json()['items']
        assert any(row['kind'] == 'technical.approval_required' and row['target'] == 'monitor' for row in notices)


def test_changed_and_dismissed_repair_targets_are_never_replayed(agent):
    identity = failed_intake(agent)
    technical_worker.cycle(force=True)
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        report, = [row for row in snapshot(owner)['issues'] if row['kind'] == 'ORDER_PROCESSING']
        assert post(owner, ROOT + f"/issues/{report['id']}/approve", {'version': 1, 'target_id': str(uuid4())}).status_code == 422
        assert post(owner, ROOT + f"/issues/{report['id']}/approve", {'version': 1}).status_code == 202
        with Session(owner_engine) as db, db.begin():
            row = db.get(EmailIntake, identity); row.status = 'DISCARDED'; row.version += 1
        technical_worker.cycle(force=True)
        with Session(owner_engine) as db:
            assert db.get(EmailIntake, identity).status == 'DISCARDED'
            assert db.get(TechnicalIssue, UUID(report['id'])).status == 'SUPERSEDED'
        assert post(owner, ROOT + f"/issues/{report['id']}/approve", {'version': 1}).status_code == 409


def test_mailbox_retry_uses_saved_settings_and_is_never_automatic(setup, monkeypatch):
    from app.intake import mailbox, service
    from test_email_delivery import connect_mailbox
    workflow = setup
    connect_mailbox(workflow['client'], monkeypatch)
    org_id = UUID(workflow['client'].get('/api/v1/auth/me').json()['organization']['id'])
    def disconnected(*args): raise mailbox.MailboxError('AUTHENTICATION')
    monkeypatch.setattr(mailbox, 'fetch_new', disconnected)
    service.poll_mailbox(org_id)
    technical_worker.cycle(force=True)
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        report, = [row for row in snapshot(owner)['issues'] if row['kind'] == 'MAILBOX_CONNECTION']
        assert report['status'] == 'AWAITING_APPROVAL'
        assert workflow['client'].get('/api/v1/companies/acme/email-intake/mailbox').json()['last_error'] == 'AUTHENTICATION'
        assert post(owner, ROOT + f"/issues/{report['id']}/approve", {'version': 1}).status_code == 202
        monkeypatch.setattr(mailbox, 'fetch_new', lambda *args: (7, 0, []))
        technical_worker.cycle(force=True)
        with Session(owner_engine) as db: assert db.get(TechnicalIssue, UUID(report['id'])).status == 'FIXED'


def test_diagnostics_are_coarse_tenant_derived_and_manual_reports_cannot_be_approved(client):
    login(client); company(client); login(client, 'dispatch', 'acme', 'dispatcher')
    path = '/api/v1/diagnostics/runtime'
    assert post(client, path, {'source': 'WEB', 'kind': 'RUNTIME_ERROR', 'message': 'Secret'}).status_code == 422
    assert post(client, path, {'source': 'DRIVER', 'kind': 'RUNTIME_ERROR'}).status_code == 403
    for _ in range(2): assert post(client, path, {'source': 'WEB', 'kind': 'RUNTIME_ERROR'}).status_code == 202
    technical_worker.cycle(force=True)
    login(client)
    report, = [row for row in snapshot(client)['issues'] if row['kind'] == 'RUNTIME_ERROR']
    assert report['company_name'] == 'Acme' and '2 runtime error' in report['description']
    assert report['action'] is None and report['status'] == 'NEEDS_ADMIN'
    assert post(client, ROOT + f"/issues/{report['id']}/approve", {'version': 1}).status_code == 409
    key = str(uuid4())
    reject = ROOT + f"/issues/{report['id']}/reject"
    dismissed = post(client, reject, {'version': 1}, key)
    assert dismissed.status_code == 200 and dismissed.json()['status'] == 'REJECTED'
    assert post(client, reject, {'version': 1}, key).json() == dismissed.json()
    technical_worker.cycle(force=True)
    assert not any(row['id'] == report['id'] for row in snapshot(client)['issues'])


def test_runtime_api_failures_and_interrupted_repairs_are_reported_without_sensitive_details(client, monkeypatch):
    from fastapi import HTTPException
    from app.agents import queries
    login(client)
    original = queries.snapshot
    monkeypatch.setattr(queries, 'snapshot', lambda db: (_ for _ in ()).throw(HTTPException(503, 'Internal private message')))
    assert client.get(ROOT + '/monitor').status_code == 503
    monkeypatch.setattr(queries, 'snapshot', original)
    with Session(owner_engine) as db, db.begin():
        db.add(TechnicalIssue(fingerprint='interrupted', kind='MAILBOX_CONNECTION', title='Interrupted retry', description='Pending', status='APPLYING',
            action='RETRY_MAILBOX', target_id=uuid4(), target_version=1, updated_at=now() - timedelta(minutes=10)))
    technical_worker.cycle(force=True)
    result = snapshot(client)
    assert 'Internal private message' not in str(result)
    assert any(row['kind'] == 'RUNTIME_ERROR' for row in result['issues'])
    assert any(row['status'] == 'FAILED' and 'interrupted' in row['description'] for row in result['issues'])


def test_integration_and_background_delivery_failures_are_reported_without_replaying(client):
    from app.integrations.models import TmsInbox
    from app.models import OutboxEvent
    login(client); org = company(client)
    org_id = UUID(org['id'])
    with Session(owner_engine) as db, db.begin():
        inbox = TmsInbox(organization_id=org_id, external_id='private-tms-id', revision='revision', received_via='WEBHOOK',
            payload={'private': 'secret'}, status='NEEDS_REVIEW')
        delivery = OutboxEvent(organization_id=org_id, event_type='push.requested', entity_id=uuid4(), attempts=3, last_error_code='PRIVATE_ERROR')
        db.add_all([inbox, delivery]); db.flush(); inbox_id, delivery_id = inbox.id, delivery.id
    technical_worker.cycle(force=True)
    result = snapshot(client)
    reports = [row for row in result['issues'] if row['kind'] in ('TMS_PROCESSING', 'BACKGROUND_DELIVERY')]
    assert len(reports) == 2 and all(row['status'] == 'NEEDS_ADMIN' and row['action'] is None for row in reports)
    assert 'secret' not in str(result) and 'private-tms-id' not in str(result) and 'PRIVATE_ERROR' not in str(result)
    with Session(owner_engine) as db:
        assert db.get(TmsInbox, inbox_id).status == 'NEEDS_REVIEW'
        assert db.get(OutboxEvent, delivery_id).attempts == 3
    technical_worker.cycle(force=True)
    assert len([row for row in snapshot(client)['issues'] if row['kind'] in ('TMS_PROCESSING', 'BACKGROUND_DELIVERY')]) == 2


def test_api_error_diagnostics_use_authenticated_company_scope(client, monkeypatch):
    from fastapi import HTTPException
    from app.operations import routes_directory
    login(client); org = company(client); login(client, 'dispatch', 'acme', 'dispatcher')
    monkeypatch.setattr(routes_directory, 'settings', lambda *args: (_ for _ in ()).throw(HTTPException(503, 'Private settings detail')))
    assert client.get('/api/v1/companies/acme/settings').status_code == 503
    with Session(owner_engine) as db:
        diagnostic = db.scalar(select(RuntimeDiagnostic).where(RuntimeDiagnostic.source == 'API'))
        assert diagnostic.organization_id == UUID(org['id']) and diagnostic.occurrences == 1


def test_technical_worker_refuses_elevated_credentials_and_serializes_cycles(client, monkeypatch):
    from sqlalchemy import text
    login(client)
    with monkeypatch.context() as patch:
        patch.setattr(technical_worker, 'engine', owner_engine)
        with pytest.raises(RuntimeError, match='Restricted runtime'):
            technical_worker.cycle(force=True)
    assert all(item['run'] is None for item in snapshot(client)['agents'])
    with engine.connect() as lock:
        lock.execute(text("SELECT pg_advisory_lock(hashtextextended('technical-agent-cycle', 0))")); lock.commit()
        try: assert technical_worker.cycle(force=True) is False
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended('technical-agent-cycle', 0))")); lock.commit()
    assert technical_worker.cycle(force=True)
    with pytest.raises(SQLAlchemyError), Session(engine) as db, db.begin():
        context(db, platform=True)
        db.query(AgentLog).update({AgentLog.message: 'Rewrite history'})


def test_agent_issue_counts_are_complete_and_scoped_pages_use_one_classification(client):
    login(client); company(client)
    with Session(owner_engine) as db, db.begin():
        for index in range(101):
            db.add(TechnicalIssue(fingerprint=f'intake:test:{index}', kind='ORDER_PROCESSING', title='Order issue', description='Review saved processing.', status='NEEDS_ADMIN'))
        db.add(TechnicalIssue(fingerprint='worker:DISPATCH:test', kind='WORKER_PROGRESS', title='Worker issue', description='Review worker.', status='APPROVED'))
        db.add(TechnicalIssue(fingerprint='runtime:test', kind='RUNTIME_ERROR', title='Runtime issue', description='Review application.', status='NEEDS_ADMIN'))
        db.add(TechnicalIssue(fingerprint='fixed:test', kind='MAILBOX_CONNECTION', title='Fixed', description='Recovered.', status='FIXED'))
    current = snapshot(client)
    assert {item['agent']: item['issue_count'] for item in current['agents']} == {'ORDER': 101, 'DISPATCH': 1, 'TECHNICAL': 1, 'OPERATIONAL': 0}
    assert len(current['issues']) == 100
    page = client.get(ROOT + '/issues', params={'agent': 'ORDER', 'limit': 1}).json()
    assert page['items'][0]['agent'] == 'ORDER' and page['next_before']
    assert client.get(ROOT + '/issues', params={'agent': 'DISPATCH', 'before': page['next_before']}).status_code == 404
    assert client.get(ROOT + '/issues', params={'agent': 'ORDER', 'before': page['next_before'], 'limit': 1}).json()['items'][0]['id'] != page['items'][0]['id']
    assert client.get(ROOT + '/issues', params={'agent': 'UNKNOWN'}).status_code == 422
