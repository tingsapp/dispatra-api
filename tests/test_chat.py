"""Order chat: participant rules, status windows, reassignment, read positions and event audiences on restricted PostgreSQL."""
from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from conftest import login, post, HEADERS
from test_manual_operations import setup, address, new_order, assign, finish, check, BASE
from test_events import sync, shipper_portal


def chats(client, order): return check(client.get(BASE + f'/orders/{order["id"]}/chats'))


def kinds(channels): return [channel['kind'] for channel in channels]


def say(client, order, kind, body, key=None, code=201):
    return check(post(client, BASE + f'/orders/{order["id"]}/chats/{kind}/messages', {'body': body}, key), code)


def thread(client, conversation, code=200): return check(client.get(BASE + f'/chats/{conversation}/messages'), code)


def message_changes(view): return [change for change in view['changes'] if change['entity'] == 'message']


def test_order_chat_participants_windows_and_reads(setup):
    w = setup
    dispatcher, driver = w['client'], w['mobile']
    order = new_order(w)
    with shipper_portal(w) as shipper:
        cursors = {name: sync(client)['cursor'] for name, client in [('dispatcher', dispatcher), ('shipper', shipper), ('driver', driver)]}
        assert kinds(chats(dispatcher, order)) == ['DISPATCH_SHIPPER'] and kinds(chats(shipper, order)) == ['DISPATCH_SHIPPER']
        assert driver.get(BASE + f'/orders/{order["id"]}/chats').status_code == 404
        assert chats(shipper, order)[0]['title'] == 'Dispatch' and chats(shipper, order)[0]['conversation_id'] is None
        # Driver chats open only once a driver is assigned.
        assert 'driver is assigned' in say(shipper, order, 'DRIVER_SHIPPER', 'Hello driver', code=409)['error']['message']

        key = str(uuid4())
        first = say(shipper, order, 'DISPATCH_SHIPPER', '  Please call before pickup.  ', key)
        assert first['body'] == 'Please call before pickup.' and first['mine'] and first['sender_role'] == 'SHIPPER'
        assert say(shipper, order, 'DISPATCH_SHIPPER', 'Please call before pickup.', key) == first
        assert post(shipper, BASE + f'/orders/{order["id"]}/chats/DISPATCH_SHIPPER/messages', {'body': 'Different'}, key).status_code == 409
        seen = sync(dispatcher, cursors['dispatcher'])
        assert [change['entity_id'] for change in message_changes(seen)] == [first['id']] and seen['unread_messages'] == 1
        assert seen['unread'] == 0, 'chat messages do not create inbox notifications'
        assert message_changes(sync(driver, cursors['driver'])) == []
        page = thread(dispatcher, first['conversation_id'])
        assert page['has_more'] is False and [m['mine'] for m in page['messages']] == [False]

        route = assign(w, order)
        order = check(dispatcher.get(BASE + f'/orders/{order["id"]}'))
        assert kinds(chats(dispatcher, order)) == ['DISPATCH_SHIPPER', 'DISPATCH_DRIVER', 'DRIVER_SHIPPER']
        assert kinds(chats(shipper, order)) == ['DISPATCH_SHIPPER', 'DRIVER_SHIPPER']
        assert kinds(chats(driver, order)) == ['DISPATCH_DRIVER', 'DRIVER_SHIPPER']
        assert [c['title'] for c in chats(driver, order)] == ['Dispatch', 'Fresh Test']
        assert [c['title'] for c in chats(shipper, order)] == ['Dispatch', 'Dana Driver']
        oversight = chats(dispatcher, order)[2]
        assert oversight['title'] == 'Dana Driver ↔ Fresh Test' and not oversight['can_send'] and 'not reply' in oversight['closed_reason']

        ops = say(dispatcher, order, 'DISPATCH_DRIVER', 'Use the loading dock.')
        assert ops['sender_role'] == 'DISPATCHER'
        assert 'not reply' in say(dispatcher, order, 'DRIVER_SHIPPER', 'Hi both', code=409)['error']['message']
        assert say(shipper, order, 'DISPATCH_DRIVER', 'Not my chat', code=404)
        thread(shipper, ops['conversation_id'], 404)
        thread(driver, first['conversation_id'], 404)
        shipper_view = sync(shipper, cursors['shipper'])
        assert ops['id'] not in {change['entity_id'] for change in message_changes(shipper_view)}

        direct = say(driver, order, 'DRIVER_SHIPPER', 'On my way.')
        assert direct['sender_name'] == 'Dana Driver'
        assert sync(shipper, cursors['shipper'])['unread_messages'] == 1
        assert direct['id'] in {change['entity_id'] for change in message_changes(sync(dispatcher, seen['cursor']))}
        assert thread(dispatcher, direct['conversation_id'])['messages'][0]['body'] == 'On my way.'
        # Oversight chats never count as dispatcher unread.
        assert sync(dispatcher, seen['cursor'])['unread_messages'] == 1
        assert sync(driver, cursors['driver'])['unread_messages'] == 1

        inbox = check(shipper.get(BASE + '/chats'))
        assert [item['kind'] for item in inbox] == ['DRIVER_SHIPPER', 'DISPATCH_SHIPPER'] and inbox[0]['unread'] == 1
        assert inbox[0]['order_number'] == order['number'] and inbox[0]['last_message']['body'] == 'On my way.'
        assert check(post(shipper, BASE + f'/chats/{direct["conversation_id"]}/read', {'seq': direct['seq'] + 1000}))['unread'] == 0
        assert check(post(shipper, BASE + f'/chats/{direct["conversation_id"]}/read', {'seq': 1}))['unread'] == 0, 'read positions never move back'
        assert check(post(dispatcher, BASE + f'/chats/{first["conversation_id"]}/read', {'seq': first['seq']}))['unread'] == 0
        assert {item['kind'] for item in check(driver.get(BASE + '/chats'))} == {'DISPATCH_DRIVER', 'DRIVER_SHIPPER'}

        finish(w, route)
        for client in (dispatcher, shipper, driver):
            assert all(not channel['can_send'] and channel['closed_reason'] for channel in chats(client, order))
        assert 'completed' in say(shipper, order, 'DRIVER_SHIPPER', 'Thanks!', code=409)['error']['message']
        assert 'completed' in say(dispatcher, order, 'DISPATCH_SHIPPER', 'Delivered.', code=409)['error']['message']
        assert [m['body'] for m in thread(driver, direct['conversation_id'])['messages']] == ['On my way.']


def test_reassignment_revokes_previous_driver_and_shippers_are_isolated(setup):
    w = setup
    dispatcher, driver = w['client'], w['mobile']
    order = new_order(w)
    route = assign(w, order)
    with shipper_portal(w) as shipper:
        old = say(driver, order, 'DRIVER_SHIPPER', 'Arriving at noon.')
        ops = say(driver, order, 'DISPATCH_DRIVER', 'Traffic on the bridge.')
        check(post(dispatcher, BASE + f'/routes/{route["id"]}/release', dict(version=route['version'], generation=route['generation'])))
        order = check(dispatcher.get(BASE + f'/orders/{order["id"]}'))
        assert order['status'] == 'NEW'
        assert driver.get(BASE + f'/orders/{order["id"]}/chats').status_code == 404
        thread(driver, old['conversation_id'], 404)
        assert check(driver.get(BASE + '/chats')) == []
        assert kinds(chats(shipper, order)) == ['DISPATCH_SHIPPER']
        thread(shipper, old['conversation_id'], 404)
        history = chats(dispatcher, order)
        assert kinds(history) == ['DISPATCH_SHIPPER', 'DISPATCH_DRIVER', 'DRIVER_SHIPPER']
        assert all(not channel['can_send'] for channel in history[1:]) and history[1]['conversation_id'] == ops['conversation_id']

        vehicle = check(post(dispatcher, BASE + '/vehicles', dict(name='Second Van', type_id=w['type_id'], plate='TEST456', province='BC',
            payload_kg=1000, volume_m3=12, length_cm=300, width_cm=180, height_cm=180, pallet_capacity=2)), 201)
        second = check(post(dispatcher, BASE + '/drivers', dict(name='Sam Second', email='sam@example.com', phone='6045550109',
            address=address(), vehicle_id=vehicle['id'])), 201)
        with TestClient(app, headers=HEADERS) as mobile:
            login(mobile, 'driver', 'acme', 'sam@example.com', second['initial_password'])
            check(post(mobile, BASE + '/driver/duty', {}), 201)
            check(post(dispatcher, BASE + f'/orders/{order["id"]}/assign', dict(version=order['version'], driver_id=second['id'],
                vehicle_id=vehicle['id'], planned_at=datetime.now(timezone.utc).isoformat())))
            fresh = chats(mobile, order)
            assert kinds(fresh) == ['DISPATCH_DRIVER', 'DRIVER_SHIPPER'] and all(c['conversation_id'] is None and c['can_send'] for c in fresh)
            thread(mobile, old['conversation_id'], 404)
            assert [c['title'] for c in chats(shipper, order)] == ['Dispatch', 'Sam Second']
            new = say(shipper, order, 'DRIVER_SHIPPER', 'Hi Sam')
            assert new['conversation_id'] != old['conversation_id']
            assert [m['body'] for m in thread(mobile, new['conversation_id'])['messages']] == ['Hi Sam']
        # Dispatchers keep both drivers' chats.
        assert len([c for c in chats(dispatcher, order) if c['kind'] == 'DRIVER_SHIPPER']) == 2

    other = check(post(dispatcher, BASE + '/shippers', {**w['shipper_body'], 'email': 'other@example.com', 'company_name': 'Other Co'}), 201)
    with TestClient(app, headers=HEADERS) as stranger:
        login(stranger, 'customer', 'acme', 'other@example.com', other['initial_password'])
        assert stranger.get(BASE + f'/orders/{order["id"]}/chats').status_code == 404
        thread(stranger, new['conversation_id'], 404)
        assert say(stranger, order, 'DISPATCH_SHIPPER', 'Hello', code=404)
        assert check(stranger.get(BASE + '/chats')) == [] and sync(stranger)['unread_messages'] == 0
