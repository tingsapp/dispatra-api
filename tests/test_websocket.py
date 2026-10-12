"""WebSocket delivery and live telemetry through the restricted PostgreSQL runtime role."""
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import text
import pytest
import time
from app.main import app
from app.events import websocket
from app.security import digest
from conftest import HEADERS, post, owner_engine
from test_manual_operations import setup, new_order, assign, check, BASE
from test_events import sync, shipper_portal, types

@pytest.fixture(autouse=True)
def fast_stream(monkeypatch):
    monkeypatch.setattr(websocket, 'interval', lambda: 0.03)
    monkeypatch.setattr(websocket, 'HEARTBEAT_SECONDS', 0.03)


def receive_change(socket, event):
    for _ in range(200):
        view = socket.receive_json()
        if event in types(view): return view
    raise AssertionError('Expected event did not arrive')


def assert_closed(socket):
    # A heartbeat already queued before revocation may still be delivered.
    with pytest.raises(WebSocketDisconnect):
        for _ in range(200): socket.receive_json()


def assert_connections_released():
    deadline = time.monotonic() + 5
    while websocket.connections and time.monotonic() < deadline: time.sleep(0.01)
    assert sum(websocket.connections.values()) == 0


def test_live_delivery_audiences_and_cursor_replay(setup):
    w = setup
    with shipper_portal(w) as shipper:
        with w['client'].websocket_connect(BASE + '/ws') as dispatcher_ws, shipper.websocket_connect(BASE + '/ws') as shipper_ws, w['mobile'].websocket_connect(BASE + '/ws') as driver_ws:
            starts = [ws.receive_json() for ws in (dispatcher_ws, shipper_ws, driver_ws)]
            assert all(view['changes'] == [] for view in starts)
            order = new_order(w)
            seen = receive_change(dispatcher_ws, 'order.created')
            assert seen['changes'][0]['entity_id'] == order['id']
            assert 'order.created' in types(receive_change(shipper_ws, 'order.created'))
            assert driver_ws.receive_json()['changes'] == []
            assign(w, order)
            assert 'order.assigned' in types(receive_change(shipper_ws, 'order.assigned'))
            driver_view = receive_change(driver_ws, 'order.assigned')
            assert driver_view['unread'] == 1
            assert all(set(change) == {'id','type','actor_type','entity','entity_id','entity_version','order_id','route_id','created_at'} for change in driver_view['changes'])
        another = new_order(w)
        with w['client'].websocket_connect(BASE + '/ws?cursor=' + seen['cursor']) as resumed:
            replay = resumed.receive_json()
            assert another['id'] in {row['entity_id'] for row in replay['changes']}


def test_origin_auth_tenant_native_and_revocation(setup):
    w = setup
    for client, path, headers in [(TestClient(app),BASE+'/ws',{'Origin':'http://testserver'}), (w['client'],BASE+'/ws',{'Origin':'https://evil.example'}), (w['client'],'/api/v1/companies/other/ws',{}), (TestClient(app),BASE+'/ws',{'Origin':'http://testserver','Authorization':'Bearer dsp_invalid'})]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path,headers=headers) as ws: ws.receive_json()
    with TestClient(app, headers=HEADERS) as native:
        native.cookies.clear()
        issued = check(native.post('/api/v1/auth/driver/login', json={'portal':'driver','organization':'acme','login_id':w['driver']['email'],'password':w['driver']['initial_password']}))
        with native.websocket_connect(BASE+'/ws',headers={'Authorization':'Bearer '+issued['token']}) as ws:
            assert ws.receive_json()['unread'] == 0
        with native.websocket_connect(BASE+'/ws',headers={'Authorization':'Bearer '+issued['token'], 'Origin':''}) as ws:
            assert ws.receive_json()['changes'] == []
    with w['client'].websocket_connect(BASE+'/ws') as ws:
        ws.receive_json()
        token=w['client'].cookies.get('dispatra_session')
        with owner_engine.begin() as db: db.execute(text('DELETE FROM login_sessions WHERE token_hash=:hash'),{'hash':digest(token)})
        assert_closed(ws)
    assert_connections_released()


def test_reset_readonly_and_connection_limit(setup,monkeypatch):
    w=setup
    with w['client'].websocket_connect(BASE+'/ws?cursor=invalid') as ws:
        assert ws.receive_json()['reset'] is True
        ws.send_text('commands are forbidden')
        assert_closed(ws)
    monkeypatch.setattr(websocket,'MAX_PER_ACCOUNT',1)
    with w['client'].websocket_connect(BASE+'/ws') as ws:
        ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with w['client'].websocket_connect(BASE+'/ws') as rejected: rejected.receive_json()
    assert_connections_released()


def test_telemetry_refreshes_monitor_and_owned_tracking_without_coordinates(setup):
    w=setup
    order=new_order(w); route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',{'version':route['version'],'generation':route['generation']}))
    with shipper_portal(w) as shipper:
        dispatcher_cursor=sync(w['client'])['cursor']; shipper_cursor=sync(shipper)['cursor']
        data={'duty_id':w['duty']['id'],'captured_at':datetime.now(timezone.utc).isoformat(),'latitude':49.27,'longitude':-123.11,'accuracy_m':5,'location_permission':'GRANTED'}
        key='telemetry-replay'
        first=check(post(w['mobile'],BASE+'/driver/location',data,key),201)
        assert check(post(w['mobile'],BASE+'/driver/location',data,key),201)==first
        seen=sync(w['client'],dispatcher_cursor)
        assert types(seen).count('driver.location_updated')==1
        assert types(seen).count('order.tracking_updated')==1
        mine=sync(shipper,shipper_cursor)
        assert types(mine)==['order.tracking_updated']
        assert 'latitude' not in str(mine) and 'longitude' not in str(mine)
        assert check(shipper.get(BASE+f'/orders/{order["id"]}/tracking'))['location']['latitude']==49.27
