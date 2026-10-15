"""Standing shipper assignments use tenant checks and the shared dispatch feasibility path."""
from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.dispatch import worker
from conftest import HEADERS, login, company, post, owner_engine
from test_manual_operations import BASE, setup, check, new_order, booking, assign, finish  # noqa: F401
from test_dispatch_agent import fleet, suggest, mode  # noqa: F401


def save(w, driver, **changes):
    current = check(w['client'].get(BASE + '/shippers/' + w['shipper']['id']))
    return w['client'].put(BASE + '/shippers/' + current['id'], json={
        'version': current['version'], 'data': w['shipper_body'] | {'assigned_driver_id': driver} | changes},
        headers={'Idempotency-Key': str(uuid4())})


def test_save_reload_clear_and_tenant_validation(fleet):
    w = fleet; client = w['client']
    assert w['shipper']['assigned_driver_id'] is None
    selected = check(save(w, w['driver']['id']))
    assert selected['assigned_driver_id'] == w['driver']['id'] and selected['assigned_driver_name'] == 'Dana Driver'
    assert check(client.get(BASE + '/shippers/' + selected['id']))['assigned_driver_id'] == w['driver']['id']
    # Older edit clients omit the field and preserve the saved assignment.
    old = check(client.put(BASE + '/shippers/' + selected['id'], json={'version': selected['version'], 'data': w['shipper_body']}, headers={'Idempotency-Key': str(uuid4())}))
    assert old['assigned_driver_id'] == w['driver']['id']
    assert save(w, str(uuid4())).status_code == 422
    with owner_engine.begin() as db:
        db.execute(text('UPDATE drivers SET active=false WHERE id=:id'), {'id': w['omar']['id']})
    assert save(w, w['omar']['id']).status_code == 422
    # Create uses the same validation and saves the choice atomically with the account.
    body = w['shipper_body'] | {'email': 'standing@example.com', 'phone': '6045550198', 'assigned_driver_id': w['driver']['id']}
    key = str(uuid4())
    created = check(post(client, BASE + '/shippers', body, key), 201)
    assert created['assigned_driver_id'] == w['driver']['id']
    replay = check(post(client, BASE + '/shippers', body, key), 201)
    assert replay['id'] == created['id'] and replay['initial_password'] is None
    assert save(w, w['driver']['id'], name='Stale', assigned_driver_id=w['driver']['id']).status_code == 200
    cleared = check(save(w, None))
    assert cleared['assigned_driver_id'] is None and cleared['assigned_driver_name'] is None
    assert 'assigned_driver' not in new_order(w)['booking']
    # Shipper self-service cannot overwrite dispatcher-managed standing assignments.
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        profile = check(portal.get(BASE + '/shipper/profile'))
        response = portal.patch(BASE + '/shipper/profile', json={'version': profile['version'], 'phone': profile['phone'], 'warehouse': profile['warehouse'], 'assigned_driver_id': w['driver']['id']}, headers={'Idempotency-Key': str(uuid4())})
        assert response.status_code == 422


def test_snapshot_is_server_owned_and_preserved_by_directory_and_order_edits(fleet):
    w = fleet; client = w['client']
    old = new_order(w)
    check(save(w, w['driver']['id']))
    body = booking(w['shipper']['id'], w['service'], w['type_id'])
    key = str(uuid4())
    order = check(post(client, BASE + '/orders', body, key), 201)
    assert order['booking']['assigned_driver'] == {'id': w['driver']['id'], 'name': 'Dana Driver'}
    assert order['status'] == 'NEW' and order['route_id'] is None
    check(save(w, w['omar']['id']))
    assert check(post(client, BASE + '/orders', body, key), 201) == order
    assert 'assigned_driver' not in check(client.get(BASE + '/orders/' + old['id']))['booking']
    edited = check(client.put(BASE + '/orders/' + order['id'], json={'version': order['version'], 'booking': order['facts']}, headers={'Idempotency-Key': str(uuid4())}))
    assert edited['booking']['assigned_driver'] == order['booking']['assigned_driver']
    fresh = new_order(w)
    assert fresh['booking']['assigned_driver']['id'] == w['omar']['id']
    # The authenticated portal gets the same server default even with an explicit conflicting soft request.
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        profile = check(portal.get(BASE + '/shipper/profile'))
        assert profile['assigned_driver_name'] == 'Omar Offduty'
        own = check(post(portal, BASE + '/orders', booking(None, w['service'], w['type_id']) | {'preferred_driver_id': w['driver']['id']}), 201)
        assert own['booking']['assigned_driver']['id'] == w['omar']['id']
    assert check(client.get(BASE + '/orders/' + order['id']))['pricing'] == edited['pricing']


def test_wrong_driver_rejected_and_auto_waits_for_assigned_driver(fleet):
    w = fleet; client = w['client']
    check(save(w, w['omar']['id']))
    order = new_order(w)
    decision = check(suggest(client, order))
    assert decision['candidates'] == []
    excluded = {item['driver_id']: item['reason'] for item in decision['excluded']}
    assert 'assigned driver' in excluded[w['driver']['id']] and 'On Duty' in excluded[w['omar']['id']]
    response = post(client, BASE + '/orders/' + order['id'] + '/assign', {'version': order['version'], 'driver_id': w['driver']['id'], 'vehicle_id': w['vehicle']['id'], 'planned_at': datetime.now(timezone.utc).isoformat()})
    assert response.status_code == 409 and 'assigned driver' in response.text
    assert check(client.get(BASE + '/orders/' + order['id']))['status'] == 'NEW'
    mode(client, 'AUTO')
    worker.cycle()
    assert check(client.get(BASE + '/orders/' + order['id']))['status'] == 'NEW'
    with TestClient(app, headers=HEADERS) as mobile:
        login(mobile, 'driver', 'acme', 'omar@example.com', w['omar']['initial_password'])
        check(post(mobile, BASE + '/driver/duty', {}), 201)
    # Dispatch retry interval is separate from the standing assignment.
    with owner_engine.begin() as db:
        db.execute(text("UPDATE dispatch_decisions SET created_at=created_at-interval '1 hour'"))
    worker.cycle()
    saved = check(client.get(BASE + '/orders/' + order['id']))
    assert saved['status'] == 'ASSIGNED'
    route = next(r for r in check(client.get(BASE + '/routes')) if r['id'] == saved['route_id'])
    assert route['driver_id'] == w['omar']['id']
    assert saved['pricing'] == order['pricing']


def test_archived_driver_assignment_remains_visible_and_waits(fleet):
    w = fleet; client = w['client']
    check(save(w, w['omar']['id']))
    check(post(client, BASE + '/drivers/' + w['omar']['id'] + '/archive', {'version': w['omar']['version']}))
    saved = check(save(w, w['omar']['id'], name='Changed contact'))
    assert saved['assigned_driver_name'] == 'Omar Offduty'
    order = new_order(w)
    assert order['booking']['assigned_driver']['id'] == w['omar']['id']
    decision = check(suggest(client, order))
    assert decision['candidates'] == []
    check(save(w, None))
    assert 'assigned_driver' not in new_order(w)['booking']
    assert check(client.get(BASE + '/orders/' + order['id']))['booking']['assigned_driver']['id'] == w['omar']['id']


def test_other_company_driver_is_rejected_and_composite_fk_is_enforced(fleet):
    from sqlalchemy.exc import IntegrityError
    import pytest
    from test_manual_operations import address
    w=fleet; client=w['client']
    login(client)
    company(client,'other')
    login(client,'dispatch','other','dispatcher')
    foreign=check(post(client,'/api/v1/companies/other/drivers',dict(name='Other Driver',email='other-driver@example.com',phone='6045550197',address=address())),201)
    login(client,'dispatch','acme','dispatcher')
    assert save(w,foreign['id']).status_code==422
    assert check(client.get(BASE+'/shippers/'+w['shipper']['id']))['assigned_driver_id'] is None
    with pytest.raises(IntegrityError), owner_engine.begin() as db:
        db.execute(text('UPDATE shippers SET assigned_driver_id=:driver WHERE id=:shipper'),{'driver':foreign['id'],'shipper':w['shipper']['id']})
    selected=check(save(w,w['driver']['id']))
    response=client.put(BASE+'/shippers/'+selected['id'],json={'version':selected['version']-1,'data':w['shipper_body']|{'assigned_driver_id':None}},headers={'Idempotency-Key':str(uuid4())})
    assert response.status_code==409
    assert check(client.get(BASE+'/shippers/'+selected['id']))['assigned_driver_id']==w['driver']['id']
