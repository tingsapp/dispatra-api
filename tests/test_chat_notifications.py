"""First shipper/driver messages notify the other party once, including concurrent sends."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from conftest import HEADERS, login, post
from app.main import app
from test_chat import say, thread
from test_events import shipper_portal, sync
from test_manual_operations import setup, new_order, assign, check, BASE
from test_push import pushes, register
from test_mobile_driver import native_login


def notes(client):
    return [n for n in check(client.get(BASE + '/notifications')) if n['kind'] == 'message.created']


@pytest.mark.parametrize('first_sender', ['SHIPPER', 'DRIVER'])
def test_first_message_in_each_direction_notifies_only_the_opposite_party(setup, first_sender):
    w = setup
    order = new_order(w)
    assign(w, order)
    with shipper_portal(w) as shipper:
        parties = {'SHIPPER': shipper, 'DRIVER': w['mobile']}
        sender = parties[first_sender]
        opposite = parties['DRIVER' if first_sender == 'SHIPPER' else 'SHIPPER']
        cursor = sync(opposite)['cursor']
        key = str(uuid4())
        first = say(sender, order, 'DRIVER_SHIPPER', 'Private gate code 4412', key)
        assert say(sender, order, 'DRIVER_SHIPPER', 'Private gate code 4412', key) == first
        assert post(sender, BASE + f'/orders/{order["id"]}/chats/DRIVER_SHIPPER/messages', {'body': 'Different'}, key).status_code == 409
        notification = notes(opposite)[0]
        assert len(notes(opposite)) == 1 and not notes(sender) and not notes(w['client'])
        assert notification['order_id'] == order['id'] and not notification['read_at']
        assert notification['title'] == ('New message from Shipper' if first_sender == 'SHIPPER' else 'New message from driver')
        assert order['number'] in notification['body'] and '4412' not in str(notification)
        view = sync(opposite, cursor)
        assert any(c['type'] == 'notification.created' and c['entity_id'] == notification['id'] for c in view['changes'])
        assert all(c['entity_id'] != notification['id'] for c in sync(w['client'], cursor)['changes'])
        assert check(post(opposite, BASE + f'/notifications/{notification["id"]}/read', {'version': notification['version']}))['read_at']
        check(post(opposite, BASE + f'/chats/{first["conversation_id"]}/read', {'seq': first['seq']}))
        say(sender, order, 'DRIVER_SHIPPER', 'Second message after read')
        assert len(notes(opposite)) == 1 and notes(opposite)[0]['read_at']
        say(opposite, order, 'DRIVER_SHIPPER', 'First reply')
        say(opposite, order, 'DRIVER_SHIPPER', 'Second reply')
        assert len(notes(sender)) == 1 and len(notes(opposite)) == 1
        # Stored recipient scope protects notices, independently of message visibility/oversight.
        assert post(sender, BASE + f'/notifications/{notification["id"]}/read', {'version': 1}).status_code == 404
        assert post(w['client'], BASE + f'/notifications/{notification["id"]}/read', {'version': 1}).status_code == 404
        assert len(thread(shipper, first['conversation_id'])['messages']) == 4


@pytest.mark.parametrize('existing_conversation', [False, True])
def test_simultaneous_first_sends_notify_once(setup, existing_conversation):
    w = setup
    order = new_order(w)
    assign(w, order)
    with shipper_portal(w) as shipper, ThreadPoolExecutor(max_workers=2) as pool:
        if existing_conversation:
            say(w['mobile'], order, 'DRIVER_SHIPPER', 'Driver started the conversation')
        futures = [pool.submit(say, shipper, order, 'DRIVER_SHIPPER', body) for body in ['First concurrent send', 'Second concurrent send']]
        sent = [future.result() for future in futures]
        assert len({message['conversation_id'] for message in sent}) == 1
        assert len(notes(w['mobile'])) == 1
        assert len(notes(shipper)) == int(existing_conversation)


def test_new_order_notifies_again_and_unrelated_accounts_cannot_read(setup):
    w = setup
    first = new_order(w)
    route = assign(w, first)
    second = new_order(w)
    assign(w, second, route)
    with shipper_portal(w) as shipper:
        say(w['mobile'], first, 'DRIVER_SHIPPER', 'First order')
        say(w['mobile'], second, 'DRIVER_SHIPPER', 'Second order')
        assert {n['order_id'] for n in notes(shipper)} == {first['id'], second['id']}
        note = notes(shipper)[0]
        other = check(post(w['client'], BASE + '/shippers', {**w['shipper_body'], 'email': 'other@example.com', 'phone': '6045550198'}), 201)
        with TestClient(app, headers=HEADERS) as stranger:
            login(stranger, 'customer', 'acme', other['email'], other['initial_password'])
            assert notes(stranger) == [] and sync(stranger)['unread'] == 0
            assert post(stranger, BASE + f'/notifications/{note["id"]}/read', {'version': 1}).status_code == 404


def test_first_chat_notification_keeps_one_existing_phone_push(setup):
    w = setup
    order = new_order(w)
    assign(w, order)
    with TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as native, shipper_portal(w) as shipper:
        native_login(native, w)
        register(native)
        for body in ['First message', 'Second message']:
            say(shipper, order, 'DRIVER_SHIPPER', body)
        assert len(notes(native)) == 1
        queued = pushes()
        assert len(queued) == 2 and all(p['kind'] == 'message.created' and p['data']['type'] == 'chat' for p in queued)
        assert all(p['data']['order_id'] == order['id'] for p in queued)


def test_failed_send_rolls_back_notice_and_chat_does_not_email(setup, monkeypatch):
    from app.events import rules, email
    w = setup
    order = new_order(w)
    assign(w, order)
    original = rules.notes
    def fail(db, action, *args):
        if action == 'message.created': raise RuntimeError('Test rollback')
        return original(db, action, *args)
    monkeypatch.setattr(rules, 'notes', fail)
    with shipper_portal(w) as shipper:
        with pytest.raises(RuntimeError, match='Test rollback'):
            say(shipper, order, 'DRIVER_SHIPPER', 'Must roll back')
        assert notes(w['mobile']) == []
        monkeypatch.setattr(rules, 'notes', original)
        def unexpected_email(*args):
            raise AssertionError('Chat must not queue an email')
        monkeypatch.setattr(email.Sender, 'queue', unexpected_email)
        sent = say(shipper, order, 'DRIVER_SHIPPER', 'Committed message')
        assert len(notes(w['mobile'])) == 1
        assert [m['body'] for m in thread(shipper, sent['conversation_id'])['messages']] == ['Committed message']
