"""Platform inbox permissions, transactions, read state and company-safe metadata on PostgreSQL."""
from uuid import uuid4, UUID

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.events.models import Notification
from app.models import User
from test_manual_operations import setup
from conftest import HEADERS, PASSWORD, company, login, post, owner_engine

ROOT = '/api/v1/platform/notifications'


def snapshot(client):
    response = client.get(ROOT)
    assert response.status_code == 200, response.text
    return response.json()


def update_profile(client, slug='acme', key=None):
    path = f'/api/v1/companies/{slug}/settings'
    original = client.get(path).json()
    return client.put(path, json={'version': original['version'], 'data': {**original['data'], 'phone': '6045550199'}},
                      headers={'Idempotency-Key': key or str(uuid4())})


def test_dispatcher_settings_notice_is_persistent_private_and_idempotent(client):
    login(client); org = company(client)
    assert snapshot(client) == {'items': [], 'unread': 0}
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        path = '/api/v1/companies/acme/settings'
        original = desk.get(path).json()
        body = {'version': original['version'], 'data': {**original['data'], 'phone': '6045550199'}}
        key = str(uuid4())
        response = desk.put(path, json=body, headers={'Idempotency-Key': key})
        assert response.status_code == 200, response.text
        assert desk.put(path, json=body, headers={'Idempotency-Key': key}).json() == response.json()
        assert desk.get(ROOT).status_code == 403
        assert desk.get('/api/v1/companies/acme/notifications').json() == []
    result = snapshot(client)
    assert result['unread'] == len(result['items']) == 1
    note, = result['items']
    assert note['organization_id'] == org['id'] and note['company_name'] == 'Acme'
    assert note['title'] == 'Company settings updated' and note['read_at'] is None
    assert '6045550199' not in str(result) and 'order_id' not in note and 'route_id' not in note
    read_key = str(uuid4())
    assert post(client, f"{ROOT}/{note['id']}/read", {'version': 999}).status_code == 409
    read = post(client, f"{ROOT}/{note['id']}/read", {'version': 1}, read_key)
    assert read.status_code == 200 and read.json()['read_at'] and read.json()['version'] == 2
    assert post(client, f"{ROOT}/{note['id']}/read", {'version': 1}, read_key).json() == read.json()
    assert snapshot(client)['unread'] == 0


def test_read_all_spans_only_owner_rows_and_non_owners_cannot_access_them(client):
    login(client); acme = company(client); company(client, 'other')
    with TestClient(app, headers=HEADERS) as desk:
        for slug in ('acme', 'other'):
            login(desk, 'dispatch', slug, 'dispatcher')
            assert update_profile(desk, slug).status_code == 200
    with Session(owner_engine) as db, db.begin():
        recipient = db.scalar(select(User.id).where(User.organization_id == UUID(acme['id']), User.role == 'DISPATCHER'))
        unrelated = Notification(organization_id=UUID(acme['id']), recipient_user_id=recipient, kind='test',
            title='Private dispatcher notice', body='Not for admin', severity='INFO')
        db.add(unrelated); db.flush(); foreign_id = str(unrelated.id)
    assert snapshot(client)['unread'] == 2
    assert post(client, f'{ROOT}/{foreign_id}/read', {'version': 1}).status_code == 404
    assert client.get(ROOT, params={'before': foreign_id}).status_code == 404
    first = client.get(ROOT, params={'limit': 1}).json()['items'][0]
    second = client.get(ROOT, params={'limit': 1, 'before': first['id']}).json()['items']
    assert len(second) == 1 and second[0]['id'] != first['id']
    key = str(uuid4())
    assert post(client, ROOT + '/read-all', {}, key).json() == {'updated': 2}
    assert post(client, ROOT + '/read-all', {}, key).json() == {'updated': 2}
    assert snapshot(client)['unread'] == 0
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        assert desk.get(ROOT).status_code == 403
        assert post(desk, ROOT + '/read-all', {}).status_code == 403
        assert post(desk, f"{ROOT}/{first['id']}/read", {'version': 1}).status_code == 403
        assert desk.get('/api/v1/companies/acme/notifications').json()[0]['read_at'] is None
    with TestClient(app, headers=HEADERS) as anonymous:
        assert anonymous.get(ROOT).status_code == 401
        assert post(anonymous, ROOT + '/read-all', {}).status_code == 401


def test_owner_changes_do_not_notify_the_owner_and_failed_settings_write_leaves_no_notice(client, monkeypatch):
    login(client); org = company(client)
    saved = client.patch(f"/api/v1/platform/organizations/{org['id']}/overview", json={'version': 1, 'name': 'Saved company', 'active': True}, headers={'Idempotency-Key': str(uuid4())})
    assert saved.status_code == 200 and snapshot(client)['unread'] == 0
    from app.operations import directory
    changed = directory.changed
    def fail(*args):
        changed(*args)
        raise HTTPException(503, 'Profile save failed.')
    monkeypatch.setattr(directory, 'changed', fail)
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        assert update_profile(desk).status_code == 503
    assert snapshot(client) == {'items': [], 'unread': 0}


def test_dispatcher_password_notice_contains_no_credentials(client):
    login(client); company(client)
    with TestClient(app, headers=HEADERS) as desk:
        login(desk, 'dispatch', 'acme', 'dispatcher')
        response = desk.post('/api/v1/auth/password', json={'current_password': PASSWORD, 'new_password': 'Changed-dispatch-secret-2026!'}, headers={'Idempotency-Key': str(uuid4())})
        assert response.status_code == 204, response.text
    note, = snapshot(client)['items']
    assert note['title'] == 'Dispatcher password changed'
    assert PASSWORD not in str(note) and 'Changed-dispatch-secret-2026!' not in str(note)


def test_critical_driver_issue_notifies_owner_and_restores_driver_scope(setup):
    from test_manual_operations import new_order, assign, check, BASE
    workflow = setup
    order = new_order(workflow)
    assign(workflow, order)
    stop = order['facts']['stops'][0]['id']
    issue = post(workflow['mobile'], BASE + f"/driver/stops/{stop}/issue", {'kind': 'UNSAFE', 'description': 'Private operational detail'})
    assert issue.status_code == 201, issue.text
    assert workflow['mobile'].get(BASE + '/driver/profile').status_code == 200
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        critical = [note for note in snapshot(owner)['items'] if note['kind'] == 'issue.reported']
        assert len(critical) == 1 and critical[0]['severity'] == 'CRITICAL'
        assert 'Private operational detail' not in str(critical) and 'order_id' not in critical[0]
        assert workflow['mobile'].get(ROOT).status_code == 403


def test_critical_system_mailbox_alert_is_transactional_and_company_labeled(client):
    from types import SimpleNamespace
    from app.services import audit
    from app.database import context
    login(client); org = company(client)
    actor = SimpleNamespace(id=UUID(login(client)['id']), organization_id=UUID(org['id']))
    with Session(owner_engine) as db, db.begin():
        context(db, actor.organization_id)
        audit(db, actor, 'mailbox.failed', uuid4(), actor.organization_id, 'SYSTEM')
    note, = snapshot(client)['items']
    assert note['kind'] == 'mailbox.failed' and note['severity'] == 'CRITICAL'
    assert note['organization_id'] == org['id'] and note['company_name'] == 'Acme'


def test_unknown_quote_email_alert_is_private_and_order_updates_are_excluded(setup, monkeypatch):
    from app.operations import email_worker
    from test_manual_operations import BASE, booking, check, new_order, assign
    from test_email_delivery import connect_mailbox
    workflow = setup
    desk = workflow['client']
    quote = check(post(desk, BASE + '/quotes', booking(None, workflow['service'], workflow['type_id'], rate=workflow['fixed'])), 201)
    check(post(desk, BASE + f"/quotes/{quote['id']}/send", {'version': quote['version'], 'recipient': 'private@example.net'}), 202)
    def ambiguous(_, account=None):
        raise email_worker.smtp_transport.UnknownAcceptance('SMTP_ACCEPTANCE_UNKNOWN')
    monkeypatch.setattr(email_worker.smtp_transport, 'send', ambiguous)
    assert email_worker.poll_once('owner-alert-test') == 1
    connect_mailbox(desk, monkeypatch)
    assign(workflow, new_order(workflow))
    assert email_worker.poll_once('owner-alert-test') == 1
    assert email_worker.poll_once('owner-alert-test') == 0
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        notes = [note for note in snapshot(owner)['items'] if note['kind'] == 'email.unknown']
        assert len(notes) == 1 and notes[0]['severity'] == 'CRITICAL'
        assert notes[0]['company_name'] == 'Acme'
        assert 'private@example.net' not in str(notes) and 'quote_id' not in notes[0]


def test_mailbox_worker_failure_notifies_owner_once_until_recovery(setup, monkeypatch):
    from app.intake import mailbox, service
    from test_email_delivery import connect_mailbox
    from test_manual_operations import BASE, check
    workflow = setup
    connect_mailbox(workflow['client'], monkeypatch)
    org_id = UUID(workflow['client'].get('/api/v1/auth/me').json()['organization']['id'])
    def disconnected(*args):
        raise mailbox.MailboxError('AUTHENTICATION')
    monkeypatch.setattr(mailbox, 'fetch_new', disconnected)
    assert service.poll_mailbox(org_id) == 0
    assert service.poll_mailbox(org_id) == 0
    assert check(workflow['client'].get(BASE + '/email-intake/mailbox'))['last_error'] == 'AUTHENTICATION'
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        notes = [note for note in snapshot(owner)['items'] if note['kind'] == 'mailbox.failed']
        assert len(notes) == 1 and notes[0]['severity'] == 'CRITICAL'
        assert 'AUTHENTICATION' not in str(notes)
        monkeypatch.setattr(mailbox, 'fetch_new', lambda *args: (7, 0, []))
        assert service.poll_mailbox(org_id) == 0
        monkeypatch.setattr(mailbox, 'fetch_new', disconnected)
        assert service.poll_mailbox(org_id) == 0
        assert len([note for note in snapshot(owner)['items'] if note['kind'] == 'mailbox.failed']) == 2
