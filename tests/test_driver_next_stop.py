"""The driver chooses which pending visit comes next; precedence, the arrived visit and capacity stay protected."""
from datetime import datetime, timezone
from uuid import uuid4
from test_manual_operations import setup, new_order, assign, check, post, BASE


def stamp():
    return datetime.now(timezone.utc).isoformat()


def started(w):
    route = None
    for _ in range(2):
        route = assign(w, check(w['client'].get(BASE + f'/orders/{new_order(w)["id"]}')), route)
    return check(post(w['mobile'], BASE + f'/driver/routes/{route["id"]}/start', dict(
        version=route['version'], generation=route['generation'], supports_live_updates=True)))


def pending(route):
    return [v for v in route['stops'] if v['status'] != 'COMPLETED']


def test_driver_moves_a_later_pickup_up_and_dispatch_hears_about_it(setup):
    w = setup; mobile, client = w['mobile'], w['client']
    route = started(w)
    url = BASE + f'/driver/routes/{route["id"]}/stops'
    first, *rest = pending(route)
    pickup = next(v for v in rest if v['stop']['kind'] == 'PICKUP')
    assert mobile.get(url + f'/{first["id"]}/next-preview').status_code == 409
    # Nothing is loaded yet, so no delivery can come next.
    blocked = next(v for v in rest if v['stop']['kind'] == 'DROPOFF')
    assert mobile.get(url + f'/{blocked["id"]}/next-preview').status_code == 409

    preview = check(mobile.get(url + f'/{pickup["id"]}/next-preview'))
    assert [v['id'] for v in preview['visits']] == [pickup['id']] + [v['id'] for v in pending(route) if v['id'] != pickup['id']]
    assert not any(v['late'] for v in preview['visits'])

    body = dict(version=route['version'], generation=route['generation'])
    key = str(uuid4())
    moved = check(post(mobile, url + f'/{pickup["id"]}/next', body, key))
    assert moved == check(post(mobile, url + f'/{pickup["id"]}/next', body, key))
    assert [v['id'] for v in moved['stops']] == [v['id'] for v in preview['visits']]
    assert moved['version'] == route['version'] + 1 and moved['plan']['revision'] == route['plan'].get('revision', 1) + 1
    assert moved['plan']['driver_moves'][-1]['stop_id'] == pickup['stop_id']
    assert moved['plan']['driver_moves'][-1]['before_stop_id'] == first['stop_id']
    assert [s['stop_id'] for s in moved['plan']['stops']] == [v['stop_id'] for v in moved['stops']]
    assert not moved['plan'].get('requires_acknowledgement')
    assert post(mobile, url + f'/{pickup["id"]}/next', dict(version=route['version'], generation=route['generation'])).status_code == 409

    notes = check(client.get(BASE + '/notifications'))
    assert any(n['title'] == 'Stop order changed' and n['body'].startswith('Dana Driver is going to the pickup for ') for n in notes)

    arrived = check(post(mobile, url + f'/{pickup["id"]}/arrive', dict(version=moved['version'], generation=moved['generation'], captured_at=stamp())))
    other = pending(arrived)[1]
    assert mobile.get(url + f'/{other["id"]}/next-preview').status_code == 409
    assert post(mobile, url + f'/{other["id"]}/next', dict(version=arrived['version'], generation=arrived['generation'])).status_code == 409


def test_next_stop_belongs_to_the_route_driver(setup):
    w = setup
    route = started(w)
    visit = pending(route)[1]
    url = BASE + f'/driver/routes/{route["id"]}/stops/{visit["id"]}'
    assert w['client'].get(url + '/next-preview').status_code in (401, 403)
    assert w['mobile'].get(BASE + f'/driver/routes/{route["id"]}/stops/{uuid4()}/next-preview').status_code == 404


def test_a_missed_window_needs_the_drivers_confirmation(setup):
    from datetime import timedelta
    from test_manual_operations import booking
    w = setup; mobile = w['mobile']
    soon, later = datetime.now(timezone.utc) + timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=3)
    urgent = booking(w['shipper']['id'], w['service'], w['type_id']); urgent['stops'][1]['window_end'] = soon.isoformat()
    waiting = booking(w['shipper']['id'], w['service'], w['type_id']); waiting['stops'][0]['window_start'] = later.isoformat()
    route = None
    for body in (urgent, waiting):
        order = check(post(w['client'], BASE + '/orders', body), 201)
        route = assign(w, check(w['client'].get(BASE + f'/orders/{order["id"]}')), route)
    route = check(post(mobile, BASE + f'/driver/routes/{route["id"]}/start', dict(version=route['version'], generation=route['generation'], supports_live_updates=True)))
    visit = next(v for v in pending(route)[1:] if v['stop_id'] == waiting['stops'][0]['id'])
    url = BASE + f'/driver/routes/{route["id"]}/stops/{visit["id"]}'
    preview = check(mobile.get(url + '/next-preview'))
    assert [v['stop_id'] for v in preview['visits'] if v['late']] == [urgent['stops'][1]['id']]
    body = dict(version=route['version'], generation=route['generation'])
    assert post(mobile, url + '/next', body).status_code == 409
    assert check(post(mobile, url + '/next', body | {'accept_late': True}))['stops'][0]['id'] == visit['id']


def test_a_drop_off_goes_next_only_after_its_pickup(setup):
    w = setup; mobile = w['mobile']
    route = started(w)
    url = BASE + f'/driver/routes/{route["id"]}/stops'
    first = pending(route)[0]
    assert first['stop']['kind'] == 'PICKUP'
    own = next(v for v in pending(route) if any(i['pickup_id'] == first['stop_id'] and i['delivery_id'] == v['stop_id'] for i in route['items']))
    later = [v for v in pending(route) if v['stop']['kind'] == 'DROPOFF']
    # Nothing is loaded yet: no destination can come next, the last stop included.
    for visit in later:
        assert mobile.get(url + f'/{visit["id"]}/next-preview').status_code == 409
        assert post(mobile, url + f'/{visit["id"]}/next', dict(version=route['version'], generation=route['generation'], accept_late=True)).status_code == 409
    route = check(post(mobile, url + f'/{first["id"]}/arrive', dict(version=route['version'], generation=route['generation'], captured_at=stamp())))
    route = check(post(mobile, url + f'/{first["id"]}/complete', dict(version=route['version'], generation=route['generation'], captured_at=stamp(),
        quantities={i['id']: i['quantity'] for i in route['items'] if i['pickup_id'] == first['stop_id']})))
    # Its goods are on board now, so its destination may go next.
    if pending(route)[0]['id'] != own['id']:
        moved = check(post(mobile, url + f'/{own["id"]}/next', dict(version=route['version'], generation=route['generation'], accept_late=True)))
        assert pending(moved)[0]['id'] == own['id']
        assert moved['plan']['driver_moves'][-1]['stop_id'] == own['stop_id']
