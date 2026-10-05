"""Restricted-role PostgreSQL checks for explicit email delivery and worker replay."""
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
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
    monkeypatch.setattr(email_worker.smtp_transport, 'send', lambda message, attachment=None: delivered.append((message, attachment)))
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
    monkeypatch.setattr(email_worker.smtp_transport, 'send', lambda message, attachment=None: delivered.append((message, attachment)))
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
    def ambiguous(_, attachment=None):
        raise email_worker.smtp_transport.UnknownAcceptance('SMTP_ACCEPTANCE_UNKNOWN')
    monkeypatch.setattr(email_worker.smtp_transport, 'send', ambiguous)
    assert email_worker.poll_once('smtp-test') == 1
    saved = check(w['client'].get(BASE + f'/email-deliveries/{queued["id"]}'))
    assert saved['status'] == 'UNKNOWN'
    assert email_worker.poll_once('smtp-test') == 0
