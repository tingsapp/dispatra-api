"""Driver push: session-bound device registration, queueing with the driver's updates, FCM delivery and token cleanup."""
import base64
import json
from uuid import uuid4
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.push import fcm, worker
from conftest import post, owner_engine
from test_manual_operations import setup, new_order, assign, check, BASE
from test_mobile_driver import native_login
from test_events import shipper_portal

TOKEN = 'fcm-token-' + 'a' * 40


def rows(sql, **params):
    with owner_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.platform', 'true', true)"))
        return [dict(row) for row in connection.execute(text(sql), params).mappings()]


def pushes(): return rows('SELECT kind, title, body, data, sent_at FROM push_messages ORDER BY created_at')


def devices(): return rows('SELECT token, user_id, platform FROM push_devices ORDER BY created_at')


def register(client, token=TOKEN, code=204):
    response = client.put(BASE + '/driver/push-device', json={'token': token, 'platform': 'ANDROID'})
    assert response.status_code == code, response.text


def test_driver_updates_and_chat_queue_pushes_only_for_a_signed_in_phone(setup):
    w = setup
    first = new_order(w)
    route = assign(w, first)
    check(post(w['client'], BASE + f'/orders/{first["id"]}/chats/DISPATCH_DRIVER/messages', {'body': 'Hello'}), 201)
    assert pushes() == [], 'no registered phone, no push'
    with TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as native:
        native_login(native, w)
        register(native)
        register(native)
        assert [d['platform'] for d in devices()] == ['ANDROID']
        assert w['client'].put(BASE + '/driver/push-device', json={'token': TOKEN, 'platform': 'ANDROID'}).status_code == 403
        assert native.put(BASE + '/driver/push-device', json={'token': 'short', 'platform': 'ANDROID'}).status_code == 422

        order = new_order(w)
        assign(w, order, route)
        assigned = pushes()
        assert [p['kind'] for p in assigned] == ['order.assigned']
        assert assigned[0]['title'] == 'Order assigned' and order['number'] in assigned[0]['body']
        assert assigned[0]['data'] == {'type': 'notification', 'notification_id': assigned[0]['data']['notification_id'], 'order_id': order['id']}

        check(post(w['client'], BASE + f'/orders/{order["id"]}/chats/DISPATCH_DRIVER/messages', {'body': 'Gate code is 4412'}), 201)
        with shipper_portal(w) as shipper:
            check(post(shipper, BASE + f'/orders/{order["id"]}/chats/DRIVER_SHIPPER/messages', {'body': 'Leave at the back door'}), 201)
        check(post(native, BASE + f'/orders/{order["id"]}/chats/DRIVER_SHIPPER/messages', {'body': 'On my way'}), 201)
        chat = [p for p in pushes() if p['kind'] == 'message.created']
        assert [p['title'] for p in chat] == ['Message from Dispatch', 'Message from Fresh Test']
        assert all(order['number'] in p['body'] and 'Gate' not in p['body'] and 'door' not in p['body'] for p in chat)
        assert chat[0]['data']['type'] == 'chat' and chat[0]['data']['order_id'] == order['id']

        assert native.post('/api/v1/auth/logout').status_code == 204
    assert devices() == [], 'the device is removed with its session'
    before = len(pushes())
    check(post(w['client'], BASE + f'/orders/{order["id"]}/chats/DISPATCH_DRIVER/messages', {'body': 'Are you there?'}), 201)
    assert len(pushes()) == before


def test_worker_sends_retries_and_drops_unregistered_tokens(setup, monkeypatch):
    w = setup
    with TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as native:
        native_login(native, w)
        register(native)
        register(native, TOKEN.replace('a', 'b'))
        assign(w, new_order(w))
        results = {TOKEN: 'RETRY', TOKEN.replace('a', 'b'): 'RETRY'}
        calls = []
        def send(credentials, client, token, title, body, data):
            calls.append((token, title, data['type']))
            return results[token]
        monkeypatch.setattr(fcm, 'send', send)
        assert worker.poll_once(None, None, 'push-test') == 1
        assert pushes()[0]['sent_at'] is None and len(calls) == 2
        retry = rows("SELECT attempts, last_error_code FROM outbox_events WHERE event_type = 'push.requested'")[0]
        assert retry == {'attempts': 1, 'last_error_code': 'FCM_UNAVAILABLE'}
        with owner_engine.begin() as connection:
            connection.execute(text("UPDATE outbox_events SET available_at = now() WHERE event_type = 'push.requested'"))
        results.update({TOKEN: 'SENT', TOKEN.replace('a', 'b'): 'UNREGISTERED'})
        assert worker.poll_once(None, None, 'push-test') == 1
        assert pushes()[0]['sent_at'] is not None
        assert [d['token'] for d in devices()] == [TOKEN]
        assert rows("SELECT published_at IS NOT NULL AS done FROM outbox_events WHERE event_type = 'push.requested'") == [{'done': True}]
        assert worker.poll_once(None, None, 'push-test') == 0


def test_a_token_moves_to_the_driver_who_registers_it_last(setup):
    w = setup
    vehicle = check(post(w['client'], BASE + '/vehicles', dict(name='Second Van', type_id=w['type_id'], plate='TEST456', province='BC',
        payload_kg=1000, volume_m3=12, length_cm=300, width_cm=180, height_cm=180, pallet_capacity=2)), 201)
    second = check(post(w['client'], BASE + '/drivers', {**w['driver_body'], 'phone':'6045550199', 'name': 'Sam Second', 'email': 'sam@example.com', 'vehicle_id': vehicle['id']}), 201)
    with TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as first, TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as other:
        native_login(first, w)
        register(first)
        native_login(other, {**w, 'driver': {**second, 'email': 'sam@example.com'}})
        register(other)
        owners = devices()
        assert len(owners) == 1 and owners[0]['user_id'] != rows("SELECT id FROM users WHERE driver_id = :d", d=w['driver']['id'])[0]['id']
        assert first.post(BASE + '/driver/push-device/remove', json={'token': TOKEN}).status_code == 204
        assert len(devices()) == 1, 'a driver cannot remove another driver’s registration'
        assert other.post(BASE + '/driver/push-device/remove', json={'token': TOKEN}).status_code == 204
        assert devices() == []


def test_fcm_signs_a_service_account_jwt_and_maps_results():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    credentials = fcm.Credentials({'project_id': 'dispatra', 'client_email': 'push@dispatra.iam.gserviceaccount.com', 'private_key': pem})
    sent, statuses = [], iter([(200, {}), (404, {}), (400, {'error': {'details': [{'errorCode': 'UNREGISTERED'}]}}), (503, {}), (400, {}), (401, {})])
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            assertion = dict(item.split('=', 1) for item in request.content.decode().split('&'))['assertion']
            header, claims, signature = assertion.split('.')
            pad = lambda part: base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
            key.public_key().verify(pad(signature), f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256())
            assert json.loads(pad(claims))['scope'] == fcm.SCOPE
            return httpx.Response(200, json={'access_token': 'ya29.test', 'expires_in': 3600})
        sent.append((request.url.path, request.headers['authorization'], json.loads(request.content)))
        status, body = next(statuses)
        return httpx.Response(status, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = [fcm.send(credentials, client, 'device', 'Order assigned', 'DCO-1 is ready.', {'type': 'notification'}) for _ in range(6)]
    assert results == ['SENT', 'UNREGISTERED', 'UNREGISTERED', 'RETRY', 'REJECTED', 'RETRY']
    path, authorization, body = sent[0]
    assert path == '/v1/projects/dispatra/messages:send' and authorization == 'Bearer ya29.test'
    assert body['message']['token'] == 'device' and body['message']['notification']['title'] == 'Order assigned'
