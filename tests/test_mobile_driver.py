"""Native bearer authentication and private assignment inbox on restricted PostgreSQL."""
from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from conftest import post, login, HEADERS, PASSWORD
from test_manual_operations import setup, new_order, assign, finish, check, BASE


def native_login(client, w):
    response = client.post('/api/v1/auth/driver/login', json={
        'organization': 'acme', 'login_id': w['driver']['email'], 'password': w['driver']['initial_password']})
    data = check(response)
    assert 'set-cookie' not in response.headers
    assert data['token'].startswith('dm_') and data['account']['role'] == 'DRIVER'
    assert datetime.fromisoformat(data['expires_at']) > datetime.now(timezone.utc)
    client.headers['Authorization'] = 'Bearer ' + data['token']
    return data


def test_native_session_delivery_and_browser_boundary(setup):
    w = setup
    with TestClient(app, headers={'X-Requested-With': 'Dispatra'}) as native:
        assert native.post('/api/v1/auth/login', json={'portal':'dispatch', 'organization':'acme', 'login_id':'dispatcher', 'password':PASSWORD}).status_code == 403
        assert native.post('/api/v1/auth/driver/login', json={'organization':'acme', 'login_id':'dispatcher', 'password':PASSWORD}).status_code == 401
        issued = native_login(native, w)
        assert check(native.get('/api/v1/auth/me'))['id'] == issued['account']['id']
        profile = check(native.get(BASE + '/driver/profile'))
        assert profile['id'] == w['driver']['id']
        assert native.get('/api/v1/companies/other/driver/profile').status_code == 404
        assert native.get(BASE + '/invoices').status_code == 404
        assert native.post('/api/v1/auth/logout', headers={'Origin':'https://untrusted.example'}).status_code == 403
        native.cookies.set('dispatra_session', issued['token'])
        assert native.get(BASE + '/driver/profile').status_code == 401
        native.cookies.clear()
        route = assign(w, new_order(w))
        assert len(check(native.get(BASE + '/driver/notifications'))) == 1
        assert finish({**w, 'mobile': native}, route)['status'] == 'COMPLETED'
        assert w['client'].get(BASE + '/invoices').status_code == 404
        assert native.post('/api/v1/auth/logout').status_code == 204
        assert native.get('/api/v1/auth/me').status_code == 401


def test_notification_ownership_replay_and_release(setup):
    w = setup
    order = new_order(w)
    body = dict(version=order['version'], driver_id=w['driver']['id'], vehicle_id=w['vehicle']['id'], planned_at=datetime.now(timezone.utc).isoformat())
    key = str(uuid4())
    first = check(post(w['client'], BASE + f'/orders/{order["id"]}/assign', body, key))
    assert check(post(w['client'], BASE + f'/orders/{order["id"]}/assign', body, key)) == first
    with TestClient(app, headers={'X-Requested-With':'Dispatra'}) as native:
        native_login(native, w)
        inbox = check(native.get(BASE + '/driver/notifications'))
        assert len(inbox) == 1
        row = inbox[0]
        assert row['order_id'] == order['id'] and row['read_at'] is None
        assert 'price' not in str(row).lower() and w['shipper']['email'] not in str(row)
        order_row = check(native.get(BASE + '/driver/orders'))[0]
        assert order_row['shipper']['phone'] == w['shipper']['phone']
        assert order_row['shipper']['email'] == w['shipper']['email']
        assert set(order_row['shipper']) == {'name', 'company_name', 'phone', 'email'}
        read_key = str(uuid4())
        read = check(post(native, BASE + f'/driver/notifications/{row["id"]}/read', {'version':row['version']}, read_key))
        assert read['read_at'] and read['version'] > row['version']
        assert check(post(native, BASE + f'/driver/notifications/{row["id"]}/read', {'version':row['version']}, read_key)) == read
        assert post(native, BASE + f'/driver/notifications/{uuid4()}/read', {'version':1}).status_code == 404
        second = check(post(w['client'], BASE + '/drivers', {**w['driver_body'], 'email':'another@example.com', 'vehicle_id':None}), 201)
        with TestClient(app, headers=HEADERS) as other:
            login(other, 'driver', 'acme', second['email'], second['initial_password'])
            assert check(other.get(BASE + '/driver/notifications')) == []
            assert post(other, BASE + f'/driver/notifications/{row["id"]}/read', {'version':read['version']}).status_code == 404
            login(other, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
            assert other.get(BASE + '/driver/notifications').status_code == 403
        route = first['route']
        check(post(w['client'], BASE + f'/routes/{route["id"]}/release', {'version':route['version'],'generation':route['generation']}))
        inbox = check(native.get(BASE + '/driver/notifications'))
        assert len(inbox) == 2 and inbox[0]['order_id'] is None
        assert check(native.get(BASE + '/driver/orders')) == []


def test_mobile_password_rotates_token_and_revokes_old_sessions(setup):
    w = setup
    with TestClient(app, headers={'X-Requested-With':'Dispatra'}) as native:
        native_login(native, w)
        changed = check(native.post('/api/v1/auth/driver/password', json={'current_password':w['driver']['initial_password'], 'new_password':'Updated-password-456!'}))
        assert native.get('/api/v1/auth/me').status_code == 401
        native.headers['Authorization'] = 'Bearer ' + changed['token']
        assert native.get('/api/v1/auth/me').status_code == 200
        assert w['mobile'].get('/api/v1/auth/me').status_code == 401
