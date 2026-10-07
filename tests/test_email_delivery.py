"""Restricted-role PostgreSQL checks for explicit email delivery and worker replay."""
import os
from uuid import uuid4
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.intake import mailbox
from app.operations import email_worker
from conftest import HEADERS, login, post, owner_engine
from test_manual_operations import BASE, booking, check, new_order, assign, finish, setup


def test_quote_email_is_queued_once_and_sent_from_snapshot(setup, monkeypatch):
    w = setup
    client = w['client']
    body = booking(None, w['service'], w['type_id'], rate=w['fixed'])
    quote = check(post(client, BASE + '/quotes', body), 201)
    path = BASE + f'/quotes/{quote["id"]}/send'
    payload = {'version': quote['version'], 'recipient': 'buyer@example.net'}
    key = str(uuid4())
    queued = check(post(client, path, payload, key), 202)
    assert queued['status'] == 'PENDING' and queued['recipient'] == 'buyer@example.net'
    assert check(post(client, path, payload, key), 202)['id'] == queued['id']
    assert post(client, path, payload).status_code == 409
    with TestClient(app, headers=HEADERS) as shipper:
        login(shipper, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        assert post(shipper, path, payload).status_code == 403
        assert shipper.get(BASE + f'/email-deliveries/{queued["id"]}').status_code == 403
    delivered = []
    monkeypatch.setattr(email_worker.smtp_transport, 'send', lambda message, attachment=None, account=None: delivered.append((message, attachment)))
    assert email_worker.poll_once('smtp-test') == 1
    assert len(delivered) == 1
    assert delivered[0][0].recipient == 'buyer@example.net'
    assert delivered[0][1] is None
    assert 'Pickup:' in delivered[0][0].body_text and 'Total:' in delivered[0][0].body_text
    assert check(client.get(BASE + f'/email-deliveries/{queued["id"]}'))['status'] == 'SENT'
    assert email_worker.poll_once('smtp-test') == 0


def test_invoice_email_uses_frozen_billing_recipient(setup, monkeypatch):
    w = setup
    client = w['client']
    order = new_order(w)
    route = assign(w, order)
    finish(w, route)
    completed = check(client.get(BASE + f'/orders/{order["id"]}'))
    invoice = check(post(client, BASE + f'/orders/{order["id"]}/invoice',
                         {'version': completed['version']}), 201)
    with owner_engine.connect() as db:
        queued_id = db.scalar(text('select id from email_deliveries where invoice_id = :id'), {'id': invoice['id']})
    assert queued_id is not None
    queued = check(client.get(BASE + f'/email-deliveries/{queued_id}'))
    assert queued['recipient'] == w['shipper']['email']
    assert queued['status'] == 'PENDING'
    assert check(post(client, BASE + f'/invoices/{invoice["id"]}/send', {'version': invoice['version']}), 202)['id'] == str(queued_id)
    delivered = []
    monkeypatch.setattr(email_worker.smtp_transport, 'send', lambda message, attachment=None, account=None: delivered.append((message, attachment)))
    assert email_worker.poll_once('smtp-test') == 1
    assert delivered[0][0].recipient == invoice['snapshot']['booking']['payer']['email']
    assert invoice['number'] in delivered[0][0].body_text
    assert delivered[0][1][0] == f"{invoice['number']}.pdf"
    assert delivered[0][1][1].startswith(b'%PDF-')
    assert check(client.get(BASE + f'/email-deliveries/{queued_id}'))['status'] == 'SENT'
    assert email_worker.poll_once('smtp-test') == 0


def test_unknown_smtp_acceptance_is_not_retried(setup, monkeypatch):
    w = setup
    quote = check(post(w['client'], BASE + '/quotes',
        booking(None, w['service'], w['type_id'], rate=w['fixed'])), 201)
    queued = check(post(w['client'], BASE + f'/quotes/{quote["id"]}/send',
        {'version': quote['version'], 'recipient': 'buyer@example.net'}), 202)
    def ambiguous(_, attachment=None, account=None):
        raise email_worker.smtp_transport.UnknownAcceptance('SMTP_ACCEPTANCE_UNKNOWN')
    monkeypatch.setattr(email_worker.smtp_transport, 'send', ambiguous)
    assert email_worker.poll_once('smtp-test') == 1
    saved = check(w['client'].get(BASE + f'/email-deliveries/{queued["id"]}'))
    assert saved['status'] == 'UNKNOWN'
    assert email_worker.poll_once('smtp-test') == 0


def shipper_inbox(w):
    with TestClient(app, headers=HEADERS) as shipper:
        login(shipper, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        return shipper.get(BASE + '/notifications')


def connect_mailbox(client, monkeypatch, **extra):
    os.environ.setdefault('MAILBOX_ENCRYPTION_KEY', Fernet.generate_key().decode())
    checked = []
    monkeypatch.setattr(mailbox, 'check', lambda *args: None)
    monkeypatch.setattr(mailbox, 'check_smtp', lambda *args: checked.append(args))
    view = check(client.put(BASE + '/email-intake/mailbox',
        json=dict(host='imap.example.com', username='orders@acme.test', password='app-password', **extra), headers={'Idempotency-Key': str(uuid4())}))
    return view, checked


def test_company_mailbox_sends_order_updates_to_shippers(setup, monkeypatch):
    w = setup
    client = w['client']
    view, checked = connect_mailbox(client, monkeypatch)
    # Gmail-style hosts need no SMTP entry: imap.* becomes smtp.* on 465, signed in with the same account.
    assert (view['smtp_host'], view['smtp_port']) == ('smtp.example.com', 465)
    assert checked == [('smtp.example.com', 465, 'orders@acme.test', 'app-password')]
    order = new_order(w)
    assign(w, order)
    sent = []
    monkeypatch.setattr(email_worker.smtp_transport, 'send', lambda message, attachment=None, account=None: sent.append((message, attachment, account)))
    assert email_worker.poll_once('smtp-test') == 1
    message, attachment, account = sent[0]
    assert message.recipient == 'alice@example.com' and message.subject.endswith('— Driver assigned') and attachment is None
    assert order['number'] in message.body_text and 'Alice Shipper' in message.body_text
    assert (account.host, account.port, account.username, account.password, account.company) == ('smtp.example.com', 465, 'orders@acme.test', 'app-password', True)
    assert account.sender_name
    with owner_engine.connect() as db:
        assert db.scalar(text("select status from email_deliveries where notification_id is not null")) == 'SENT'
    # A Shipper who turns order emails off still gets inbox notifications, but no email.
    body = dict(w['shipper_body'], email_updates=False)
    shipper = check(client.get(BASE + f'/shippers/{w["shipper"]["id"]}'))
    assert shipper['email_updates'] is True
    assert check(client.put(BASE + f'/shippers/{shipper["id"]}', json={'version': shipper['version'], 'data': body}, headers={'Idempotency-Key': str(uuid4())}))['email_updates'] is False
    fresh = new_order(w)
    check(post(client, BASE + f'/orders/{fresh["id"]}/cancel', {'version': fresh['version']}))
    assert 'Order cancelled' in [n['title'] for n in check(shipper_inbox(w))]
    assert email_worker.poll_once('smtp-test') == 0


def test_failed_order_update_email_warns_dispatchers(setup, monkeypatch):
    w = setup
    connect_mailbox(w['client'], monkeypatch, smtp_host='mail.example.com', smtp_port=587)
    assign(w, new_order(w))
    def refused(message, attachment=None, account=None):
        assert (account.host, account.port) == ('mail.example.com', 587)
        raise email_worker.smtp_transport.PermanentFailure('SMTP_AUTHENTICATION')
    monkeypatch.setattr(email_worker.smtp_transport, 'send', refused)
    assert email_worker.poll_once('smtp-test') == 1
    titles = [n['title'] for n in check(w['client'].get(BASE + '/notifications'))]
    assert 'Order update email not sent' in titles
