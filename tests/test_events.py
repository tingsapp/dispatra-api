"""Event feed, audience isolation, notification inbox and gap-free cursors on restricted PostgreSQL."""
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.events import feed
from app.main import app
from conftest import login, post, HEADERS, owner_engine
from test_manual_operations import setup, booking, new_order, assign, check, BASE


def sync(client, cursor=None):
    return check(client.get(BASE + '/sync', params={'cursor': cursor} if cursor else None))


def types(view): return [change['type'] for change in view['changes']]


def shipper_portal(w):
    portal = TestClient(app, headers=HEADERS)
    login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
    return portal


def test_sync_audiences_and_notifications(setup):
    w = setup
    dispatcher, driver = w['client'], w['mobile']
    with shipper_portal(w) as portal:
        start = {name: sync(client) for name, client in [('dispatcher', dispatcher), ('shipper', portal), ('driver', driver)]}
        assert all(view['changes'] == [] and not view['reset'] and view['next_poll_ms'] == 15000 for view in start.values())

        own = check(post(portal, BASE + '/orders', booking(None, w['service'], w['type_id'])), 201)
        seen = sync(dispatcher, start['dispatcher']['cursor'])
        assert types(seen) == ['order.created', 'notification.created']
        assert seen['changes'][0]['entity_id'] == own['id'] and seen['changes'][0]['actor_type'] == 'USER'
        assert seen['unread'] == 1
        note = check(dispatcher.get(BASE + '/notifications'))[0]
        assert note['title'] == 'New order booked' and own['number'] in note['body'] and note['order_id'] == own['id']
        assert 'price' not in note['body'].lower() and w['shipper']['email'] not in note['body']
        assert sync(dispatcher, seen['cursor'])['changes'] == []

        mine = sync(portal, start['shipper']['cursor'])
        assert types(mine) == ['order.created'] and mine['unread'] == 0
        assert check(portal.get(BASE + '/notifications')) == []
        assert sync(driver, start['driver']['cursor'])['changes'] == []

        route = assign(w, own)
        shipper_view = sync(portal, mine['cursor'])
        assert 'order.assigned' in types(shipper_view) and 'route.created' not in types(shipper_view)
        assert [n['title'] for n in check(portal.get(BASE + '/notifications'))] == ['Driver assigned']
        driver_view = sync(driver, start['driver']['cursor'])
        assert {'order.assigned', 'route.created', 'notification.created'} <= set(types(driver_view))
        assert driver_view['unread'] == 1
        # The acting dispatcher is not notified about their own assignment.
        assert sync(dispatcher, seen['cursor'])['unread'] == 1

        unread = check(portal.get(BASE + '/notifications'))[0]
        key = str(uuid4())
        read = check(post(portal, BASE + f'/notifications/{unread["id"]}/read', {'version': unread['version']}, key))
        assert read['read_at'] and check(post(portal, BASE + f'/notifications/{unread["id"]}/read', {'version': unread['version']}, key)) == read
        after_read = sync(portal, shipper_view['cursor'])
        assert types(after_read) == ['notification.read'] and after_read['unread'] == 0
        assert post(portal, BASE + f'/notifications/{note["id"]}/read', {'version': 1}).status_code == 404
        assert post(dispatcher, BASE + f'/notifications/{unread["id"]}/read', {'version': 1}).status_code == 404

        assert check(post(dispatcher, BASE + '/notifications/read-all', {}))['updated'] == 1
        assert check(post(dispatcher, BASE + '/notifications/read-all', {}))['updated'] == 0
        assert sync(dispatcher, seen['cursor'])['unread'] == 0
        assert check(driver.get(BASE + '/driver/notifications'))[0]['title'] == 'Order assigned'
        assert route['id'] in {change['route_id'] for change in driver_view['changes']}


def test_cursor_reset_tenant_isolation_and_rollback(setup):
    w = setup
    dispatcher = w['client']
    assert sync(dispatcher, 'not-a-cursor')['reset'] is True
    assert sync(dispatcher, '99999999999999-0')['reset'] is True
    cursor = sync(dispatcher)['cursor']
    order = new_order(w)
    assert post(dispatcher, BASE + f'/orders/{order["id"]}/cancel', {'version': order['version'] + 5}).status_code == 409
    view = sync(dispatcher, cursor)
    assert types(view) == ['order.created']
    with TestClient(app, headers=HEADERS) as owner:
        login(owner)
        check(post(owner, '/api/v1/platform/organizations', dict(name='Other', slug='other', admin_login='dispatcher', password='Initial-password-123!')), 201)
        login(owner, 'dispatch', 'other', 'dispatcher')
        foreign = check(owner.get('/api/v1/companies/other/sync'))
        own_events = check(owner.get('/api/v1/companies/other/sync', params={'cursor': cursor}))['changes']
        assert types({'changes': own_events}) == ['pricing.seeded'] and order['id'] not in str(own_events)
        assert owner.get(BASE + '/sync').status_code == 404
        assert foreign['unread'] == 0


def _insert(db, organization_id):
    identity = uuid4()
    db.execute(text("""INSERT INTO events (id, organization_id, type, actor_type, entity, entity_id, dispatchers, correlation_id, created_at)
        VALUES (:id, :org, 'test.event', 'SYSTEM', 'test', :id, true, :id, now())"""), {'id': identity, 'org': organization_id})
    return identity


def test_feed_waits_for_lower_sequence_and_consumers_are_independent(setup):
    with owner_engine.connect() as connection:
        organization_id = connection.scalar(text("SELECT id FROM organizations WHERE slug = 'acme'"))
    with Session(owner_engine) as reader, reader.begin():
        start = feed.parse(reader, feed.head(reader))
        assert feed.consume(reader, organization_id, 'first')[0] == []
        assert feed.consume(reader, organization_id, 'second')[0] == []
    slow = Session(owner_engine); slow.begin()
    first = _insert(slow, organization_id)
    with Session(owner_engine) as fast, fast.begin(): second = _insert(fast, organization_id)
    with Session(owner_engine) as reader, reader.begin():
        assert feed.read(reader, organization_id, start, 10, "type = 'test.event'")[0] == []
    slow.commit(); slow.close()
    with Session(owner_engine) as reader, reader.begin():
        rows, position = feed.read(reader, organization_id, start, 10, "type = 'test.event'")
        assert [row['id'] for row in rows] == [first, second]
        events, after = feed.consume(reader, organization_id, 'first')
        assert [row['id'] for row in events if row['type'] == 'test.event'] == [first, second]
        feed.advance(reader, organization_id, 'first', after)
    with Session(owner_engine) as reader, reader.begin():
        assert feed.consume(reader, organization_id, 'first')[0] == []
        assert [row['id'] for row in feed.consume(reader, organization_id, 'second')[0] if row['type'] == 'test.event'] == [first, second]
