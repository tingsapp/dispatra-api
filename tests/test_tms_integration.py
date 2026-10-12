"""Connected TMS on real PostgreSQL: connection settings, secrets, field maps, host safety and tenancy.

TMS HTTP calls and DNS are replaced by in-process fakes; no external calls are made.
"""
import os
import socket
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from conftest import login, company, post, owner_engine

os.environ['MAILBOX_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
from app.integrations import client as tms_client  # noqa: E402

BASE = '/api/v1/companies/acme/integrations/tms'
CREDENTIAL = 'tms-api-key-0001'
SECRET = 'whsec_' + 'a' * 64
ADDRESSES = {'api.tms.example': '93.184.216.34', 'internal.tms.example': '10.0.0.5', 'hooks.shipper.example': '93.184.216.35'}


def check(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def body(**changes):
    value = dict(enabled=True, base_url='https://api.tms.example/v1', auth_type='api_key', credential=CREDENTIAL, webhook_secret=SECRET,
        field_maps={'driver': {'id': 'driverId', 'name': 'firstName', 'email': 'email'}})
    value.update(changes)
    return value


def put(client, data, key=None):
    from uuid import uuid4
    return client.put(BASE, json=data, headers={'Idempotency-Key': key or str(uuid4())})


@pytest.fixture
def tms(client, monkeypatch):
    def resolve(host, port, *args, **kwargs):
        if host not in ADDRESSES: raise socket.gaierror('unknown')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ADDRESSES[host], port))]
    monkeypatch.setattr(tms_client.socket, 'getaddrinfo', resolve)
    calls = []
    def request(method, target, auth, body=None):
        calls.append((method, target, auth))
        if 'bad' in target: raise tms_client.TmsError('UNAUTHORIZED', 401)
        return {'data': []}
    monkeypatch.setattr(tms_client, 'request', request)
    login(client); company(client); company(client, 'other')
    login(client, 'dispatch', 'acme', 'dispatcher')
    return calls


def test_connection_saves_encrypted_secrets_and_reports_missing_required_fields(client, tms):
    assert check(client.get(BASE)) is None
    check(put(client, body(credential=None)), 422)
    check(put(client, body(webhook_secret=None)), 422)
    saved = check(put(client, body()))
    assert saved['credential_configured'] and saved['webhook_secret_configured'] and saved['version'] == 1
    assert 'credential' not in saved and 'webhook_secret' not in saved
    assert saved['status_endpoint'] == '/orders/{id}/status' and saved['fleet_sync_minutes'] == 60
    assert saved['missing_fields']['driver'] == ['address', 'city', 'phone', 'postal_code', 'province']
    assert 'item.weight' in saved['missing_fields']['order'] and saved['missing_fields']['vehicle']
    with owner_engine.connect() as db:
        row = db.execute(text('SELECT credential, webhook_secret FROM tms_connections')).one()
    assert CREDENTIAL not in row.credential and SECRET not in row.webhook_secret
    # Omitting secrets keeps them; a stale version is refused.
    kept = check(put(client, body(credential=None, webhook_secret=None, version=1, fleet_sync_minutes=360)))
    assert kept['credential_configured'] and kept['fleet_sync_minutes'] == 360 and kept['version'] == 2
    check(put(client, body(version=1)), 409)
    with owner_engine.connect() as db:
        assert db.execute(text('SELECT credential FROM tms_connections')).scalar() == row.credential


def test_input_rejects_unsafe_addresses_bad_maps_and_status_paths(client, tms):
    check(put(client, body(base_url='http://api.tms.example')), 422)
    check(put(client, body(base_url='https://user:pass@api.tms.example')), 422)
    assert check(put(client, body(base_url='https://internal.tms.example')), 422)['error']['message'] == 'TMS address is not allowed.'
    assert check(put(client, body(base_url='https://missing.tms.example')), 422)['error']['message'] == 'TMS address was not found.'
    check(put(client, body(field_maps={'driver': {'salary': 'pay'}})), 422)
    check(put(client, body(field_maps={'order': {'shipper.name': 'customer name'}})), 422)
    check(put(client, body(status_endpoint='/orders/status')), 422)
    check(put(client, body(drivers_endpoint='drivers')), 422)
    # Literal value rows accept any text; blank rows are dropped.
    saved = check(put(client, body(field_maps={'order': {'stop.pickup_value': 'PU / Pickup', 'notes': '  '}})))
    assert saved['field_maps']['order'] == {'stop.pickup_value': 'PU / Pickup'}


def test_connection_test_reads_drivers_with_the_saved_credential(client, tms):
    check(client.post(BASE + '/test', json={}), 404)
    check(put(client, body(auth_type='bearer')))
    assert check(client.post(BASE + '/test', json={})) == {'ok': True, 'message': 'Connected.'}
    assert tms[-1] == ('GET', 'https://api.tms.example/v1/drivers', {'Accept': 'application/json', 'User-Agent': 'Dispatra-TMS-Connector/1', 'Authorization': f'Bearer {CREDENTIAL}'})
    check(put(client, body(base_url='https://api.tms.example/bad', credential=None, webhook_secret=None, version=1)))
    assert check(client.post(BASE + '/test', json={})) == {'ok': False, 'message': 'The TMS rejected the API key or token.'}


def test_connections_are_company_dispatcher_only(client, tms):
    check(put(client, body()))
    login(client, 'dispatch', 'other', 'dispatcher')
    assert check(client.get(BASE.replace('acme', 'other'))) is None
    assert client.get(BASE).status_code in {403, 404}
    with owner_engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM tms_connections')).scalar() == 1


def test_client_refuses_private_hosts_and_plain_http(monkeypatch):
    monkeypatch.setattr(tms_client.socket, 'getaddrinfo', lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))])
    with pytest.raises(tms_client.TmsError) as refused: tms_client.check_host('https://localhost/api')
    assert refused.value.code == 'HOST_NOT_ALLOWED'
    with pytest.raises(tms_client.TmsError) as plain: tms_client.request('GET', 'http://api.tms.example/x', {})
    assert plain.value.code == 'HTTPS_REQUIRED'


# ---- Fleet sync -------------------------------------------------------------------------------------------------

from app.intake import geocode  # noqa: E402
from app.integrations import fleet  # noqa: E402

DRIVER_MAP = {'id': 'driverId', 'name': 'fullName', 'email': 'email', 'phone': 'mobile', 'address': 'home.street', 'city': 'home.city',
    'province': 'home.province', 'postal_code': 'home.postal', 'vehicle_unit': 'truck', 'active': 'status'}
VEHICLE_MAP = {'id': 'unitId', 'unit_number': 'unit', 'vehicle_type': 'equipment', 'plate': 'plate', 'province': 'plateRegion', 'payload': 'maxWeight'}


def tms_driver(number, truck='T-12', **changes):
    value = {'driverId': f'DRV-{number}', 'fullName': f'Driver {number}', 'email': f'driver{number}@fleet.example', 'mobile': f'60455501{number:02d}',
        'home': {'street': f'{number} Kingsway', 'city': 'Vancouver', 'province': 'British Columbia', 'postal': 'v5n2t4'}, 'truck': truck, 'status': 'ACTIVE'}
    value.update(changes)
    return value


@pytest.fixture
def fleet_tms(client, tms, monkeypatch):
    """A fake TMS: vehicles and drivers endpoints serve whatever the test puts in `data`; drivers are paged by cursor."""
    catalog = check(client.get('/api/v1/companies/acme/catalog'))
    van = next(c for c in catalog if c['code'] == 'veh_1_ton')
    data = {'vehicles': [{'unitId': 'U-12', 'unit': 't-12', 'equipment': van['data']['name'].upper(), 'plate': 'nxt 482', 'plateRegion': 'BC', 'maxWeight': '2,000'},
            {'unitId': 'U-21', 'unit': 'T-21', 'equipment': 'REEFER', 'plate': 'RFX 220', 'plateRegion': 'BC'}],
        'drivers': [tms_driver(1), tms_driver(2, truck=None), tms_driver(3, home={'street': '9 Main', 'city': 'Vancouver', 'province': 'BC', 'postal': ''})]}
    geocoded = []
    monkeypatch.setattr(geocode, 'verify', lambda text_, postal: geocoded.append(text_) or {'text': text_, 'latitude': 49.26, 'longitude': -123.07})
    def request(method, target, auth, body=None):
        tms.append((method, target, auth))
        if target.endswith('/vehicles'): return {'data': data['vehicles']}
        if target.endswith('/drivers'): return {'data': data['drivers'][:2], 'next_cursor': 'p2'} if len(data['drivers']) > 2 else {'data': data['drivers']}
        if target.endswith('/drivers?cursor=p2'): return {'data': data['drivers'][2:]}
        raise tms_client.TmsError('NOT_FOUND', 404)
    monkeypatch.setattr(tms_client, 'request', request)
    check(put(client, body(field_maps={'driver': DRIVER_MAP, 'vehicle': VEHICLE_MAP})))
    return {'data': data, 'geocoded': geocoded, 'van': van}


def directory(client, kind):
    return check(client.get(f'/api/v1/companies/acme/{kind}'))


def test_sync_imports_matched_records_and_reports_the_rest(client, fleet_tms):
    result = check(client.post(BASE + '/sync', json={}))
    assert result['vehicles'] == {'created': 1, 'updated': 0, 'unchanged': 0, 'skipped': 1, 'archived': 0}
    assert result['drivers'] == {'created': 2, 'updated': 0, 'unchanged': 0, 'skipped': 1, 'archived': 0}
    reasons = {(issue['kind'], issue['id']): issue['reason'] for issue in result['issues']}
    assert 'REEFER' in reasons[('vehicle', 'U-21')] and 'Canadian address' in reasons[('driver', 'DRV-3')]
    vehicle = next(v for v in directory(client, 'vehicles') if v['data']['unit_number'] == 'T-12')
    # 2,000 lb payload converted with the company weight unit; size comes from the vehicle type.
    assert vehicle['plate'] == 'NXT 482' and vehicle['data']['payload_kg'] == '907.18'
    assert vehicle['data']['length_cm'] == fleet_tms['van']['data']['length_cm'] and vehicle['type_id'] == fleet_tms['van']['id']
    drivers = {d['email']: d for d in directory(client, 'drivers')}
    first = drivers['driver1@fleet.example']
    assert first['vehicle_id'] == vehicle['id'] and first['address']['postal_code'] == 'V5N 2T4' and first['address']['province'] == 'BC'
    assert first['address']['latitude'] == 49.26 and drivers['driver2@fleet.example']['vehicle_id'] is None
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM users WHERE role = 'DRIVER' AND login_id LIKE '%@fleet.example' AND active")).scalar() == 2
        assert db.execute(text("SELECT actor_type FROM events WHERE type = 'driver.created' LIMIT 1")).scalar() == 'SYSTEM'
        assert db.execute(text("SELECT count(*) FROM tms_links")).scalar() == 3
    saved = check(client.get(BASE))
    assert saved['last_fleet_sync_at'] and saved['last_fleet_result']['drivers']['created'] == 2


def test_resync_skips_unchanged_records_updates_changes_and_archives_removed_ones(client, fleet_tms):
    check(client.post(BASE + '/sync', json={}))
    geocoded = len(fleet_tms['geocoded'])
    again = check(client.post(BASE + '/sync', json={}))
    assert again['drivers']['unchanged'] == 2 and again['vehicles']['unchanged'] == 1 and len(fleet_tms['geocoded']) == geocoded
    data = fleet_tms['data']
    data['drivers'] = [tms_driver(1, mobile='6045559999')]
    changed = check(client.post(BASE + '/sync', json={}))
    assert changed['drivers'] == {'created': 0, 'updated': 1, 'unchanged': 0, 'skipped': 0, 'archived': 1}
    drivers = {d['email']: d for d in directory(client, 'drivers')}
    assert drivers['driver1@fleet.example']['phone'] == '6045559999' and 'driver2@fleet.example' not in drivers
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT archived_at IS NOT NULL FROM drivers WHERE email = 'driver2@fleet.example'")).scalar()
    # An empty answer never archives the fleet.
    data['drivers'], data['vehicles'] = [], []
    empty = check(client.post(BASE + '/sync', json={}))
    assert empty['drivers']['archived'] == 0 and empty['vehicles']['archived'] == 0
    assert 'driver1@fleet.example' in {d['email'] for d in directory(client, 'drivers')}


def test_sync_links_existing_dispatra_records_instead_of_duplicating(client, fleet_tms):
    van = fleet_tms['van']
    existing = check(post(client, '/api/v1/companies/acme/vehicles', dict(name='Old', unit_number='T-12', type_id=van['id'], plate='OLD1', province='BC',
        payload_kg=500, length_cm=300, width_cm=180, height_cm=180)), 201)
    person = check(post(client, '/api/v1/companies/acme/drivers', dict(name='Existing', email='driver1@fleet.example', phone='6045550000',
        address=dict(text='1 Main Street, Vancouver, BC V5Y 1V4', city='Vancouver', province='BC', postal_code='V5Y 1V4'), crew_size=2)), 201)
    result = check(client.post(BASE + '/sync', json={}))
    assert result['vehicles']['created'] == 0 and result['vehicles']['updated'] == 1
    assert result['drivers']['created'] == 1 and result['drivers']['updated'] == 1
    assert [v['id'] for v in directory(client, 'vehicles') if v['data']['unit_number'] == 'T-12'] == [existing['id']]
    linked = next(d for d in directory(client, 'drivers') if d['id'] == person['id'])
    assert linked['name'] == 'Driver 1' and linked['data']['crew_size'] == 2 and linked['vehicle_id'] == existing['id']


def test_sync_errors_and_access(client, fleet_tms, monkeypatch):
    monkeypatch.setattr(tms_client, 'request', lambda *args, **kwargs: (_ for _ in ()).throw(tms_client.TmsError('UNAUTHORIZED', 401)))
    assert check(client.post(BASE + '/sync', json={}), 502)['error']['message'] == 'The TMS rejected the API key or token.'
    assert check(client.get(BASE))['last_error'] == 'UNAUTHORIZED'
    check(put(client, body(enabled=False, credential=None, webhook_secret=None, version=check(client.get(BASE))['version'])))
    check(client.post(BASE + '/sync', json={}), 409)
    login(client, 'dispatch', 'other', 'dispatcher')
    check(client.post(BASE.replace('acme', 'other') + '/sync', json={}), 404)


# ---- Order intake -----------------------------------------------------------------------------------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.integrations import order_intake  # noqa: E402

ORDER_MAP = {'id': 'orderId', 'scheduled_at': 'pickupDate', 'service': 'serviceLevel', 'total': 'charges.total', 'tax': 'charges.tax',
    'notes': 'notes', 'status': 'state', 'cancelled_value': 'CANCELLED', 'shipper.id': 'customer.id', 'shipper.name': 'customer.name',
    'shipper.email': 'customer.email', 'stops': 'stops', 'stop.id': 'ref', 'stop.type': 'type', 'stop.pickup_value': 'PU', 'stop.delivery_value': 'DEL',
    'stop.address': 'street', 'stop.city': 'city', 'stop.province': 'prov', 'stop.postal_code': 'zip', 'items': 'lines', 'item.pickup_stop': 'from',
    'item.delivery_stop': 'to', 'item.quantity': 'qty', 'item.weight': 'lbs', 'item.length': 'l', 'item.width': 'w', 'item.height': 'h'}


def stop(ref, kind, street):
    return {'ref': ref, 'type': kind, 'street': street, 'city': 'Vancouver', 'prov': 'BC', 'zip': 'V6B 1A1'}


def tms_order(order_id='TMS-1', **changes):
    value = {'orderId': order_id, 'pickupDate': '2026-10-12T09:00:00', 'serviceLevel': 'same_day', 'charges': {'total': 210.0, 'tax': 10.0},
        'notes': 'Dock 4', 'state': 'OPEN', 'customer': {'id': 'C-1', 'name': 'Northwind Foods', 'email': 'Dispatch@Northwind.example'},
        'stops': [stop('P1', 'PU', '1 Pickup Street'), stop('P2', 'PU', '2 Pickup Street'), stop('D1', 'DEL', '3 Drop Street'), stop('D2', 'DEL', '4 Drop Street')],
        'lines': [{'from': 'P1', 'to': 'D2', 'qty': 2, 'lbs': 10, 'l': 12, 'w': 10, 'h': 8}, {'from': 'P2', 'to': 'D1', 'qty': 1, 'lbs': 20, 'l': 24, 'w': 20, 'h': 16}]}
    value.update(changes)
    return value


def signature(payload: bytes, secret=SECRET):
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@pytest.fixture
def intake(client, tms, monkeypatch):
    monkeypatch.setattr(geocode, 'verify', lambda text_, postal: {'text': text_, 'latitude': 49.28, 'longitude': -123.11})
    check(post(client, '/api/v1/companies/acme/rate-cards', {'code': 'TMS', 'data': {'name': 'Imported price (TMS)', 'method': 'IMPORTED'}}), 201)
    check(put(client, body(field_maps={'order': ORDER_MAP})))
    tms_side = TestClient(app)  # a TMS has no session, Origin or X-Requested-With
    def send(payload, secret=SECRET, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        return tms_side.post('/api/v1/companies/acme/integrations/tms/events', content=data,
            headers={'X-Signature': 'sha256=' + signature(data, secret), 'Content-Type': 'application/json'})
    with owner_engine.connect() as db:
        organization = db.execute(text("SELECT id FROM organizations WHERE slug = 'acme'")).scalar()
    yield {'send': send, 'org': organization, 'tms_side': tms_side}
    tms_side.close()


def inbox(client, status='all'):
    return check(client.get(BASE + f'/inbox?status={status}'))


def orders(client):
    return check(client.get('/api/v1/companies/acme/orders'))


def test_webhook_requires_a_valid_signature_and_stores_each_payload_once(client, intake):
    data = json.dumps(tms_order()).encode()
    unsigned = intake['tms_side'].post('/api/v1/companies/acme/integrations/tms/events', content=data, headers={'Content-Type': 'application/json'})
    assert unsigned.status_code == 401
    assert intake['send'](tms_order(), secret='whsec_' + 'b' * 64).status_code == 401
    assert intake['tms_side'].post('/api/v1/companies/missing/integrations/tms/events', content=data, headers={'X-Signature': signature(data)}).status_code == 401
    assert check(intake['send'](tms_order()), 202) == {'accepted': 1, 'rejected': 0}
    assert check(intake['send'](tms_order()), 202) == {'accepted': 0, 'rejected': 0}
    assert check(intake['send']({'orders': [tms_order('TMS-2'), {'nothing': 1}]}), 202) == {'accepted': 1, 'rejected': 1}
    assert intake['send'](None, raw=b'not json').status_code == 422
    assert [row['status'] for row in inbox(client)] == ['RECEIVED', 'RECEIVED']
    check(put(client, body(sync_method='polling', credential=None, webhook_secret=None, field_maps={'order': ORDER_MAP}, version=check(client.get(BASE))['version'])))
    assert intake['send'](tms_order('TMS-3')).status_code == 409


def test_multi_stop_order_is_created_with_imported_price_and_a_matched_or_new_shipper(client, intake):
    check(intake['send'](tms_order()), 202)
    assert order_intake.process_pending(intake['org']) == 1
    [row] = inbox(client)
    assert row['status'] == 'ORDER_CREATED' and row['order_number']
    [order] = [o for o in orders(client) if o['id'] == row['order_id']]
    assert order['source'] == 'IMPORT' and order['facts']['external_reference'] == 'TMS-1' and order['facts']['internal_notes'] == 'Dock 4'
    assert order['pricing']['status'] == 'PRICED' and order['pricing']['total'] == '210.00'
    stops = {s['address']['text'].split(',')[0]: s for s in order['facts']['stops']}
    items = order['facts']['items']
    assert items[0]['pickup_id'] == stops['1 Pickup Street']['id'] and items[0]['delivery_id'] == stops['4 Drop Street']['id']
    assert items[1]['pickup_id'] == stops['2 Pickup Street']['id'] and items[1]['delivery_id'] == stops['3 Drop Street']['id']
    assert items[0]['weight_kg'] == '4.54' and items[0]['length_cm'] == '30.48' and items[0]['quantity'] == 2
    from datetime import datetime, timezone
    assert datetime.fromisoformat(order['scheduled_at']) == datetime(2026, 10, 12, 16, tzinfo=timezone.utc)  # company zone applied to a naive TMS time
    shippers = check(client.get('/api/v1/companies/acme/shippers'))
    created = next(s for s in shippers if s['email'] == 'dispatch@northwind.example')
    assert order['shipper_id'] == created['id'] and created['warehouse']['text'].startswith('1 Pickup Street')
    # A later order from the same TMS customer reuses the linked shipper.
    check(intake['send'](tms_order('TMS-9')), 202); order_intake.process_pending(intake['org'])
    assert len([s for s in check(client.get('/api/v1/companies/acme/shippers')) if s['email'] == 'dispatch@northwind.example']) == 1


def test_updates_keep_identities_cancellations_apply_and_late_payloads_never_roll_back(client, intake):
    check(intake['send'](tms_order()), 202); order_intake.process_pending(intake['org'])
    first = next(o for o in orders(client) if o['facts']['external_reference'] == 'TMS-1')
    check(intake['send'](tms_order(notes='Use the side door')), 202); order_intake.process_pending(intake['org'])
    updated = next(o for o in orders(client) if o['id'] == first['id'])
    assert updated['facts']['internal_notes'] == 'Use the side door' and updated['version'] > first['version']
    assert [s['id'] for s in updated['facts']['stops']] == [s['id'] for s in first['facts']['stops']]
    # Retrying the older payload after the newer one was applied is ignored.
    old = next(row for row in inbox(client) if row['status'] == 'ORDER_CREATED')
    with owner_engine.begin() as db: db.execute(text("UPDATE tms_inbox SET status = 'NEEDS_REVIEW' WHERE id = :id"), {'id': old['id']})
    check(post(client, BASE + f'/inbox/{old["id"]}/retry', {}))
    order_intake.process_pending(intake['org'])
    assert next(row for row in inbox(client) if row['id'] == old['id'])['status'] == 'IGNORED'
    assert next(o for o in orders(client) if o['id'] == first['id'])['facts']['internal_notes'] == 'Use the side door'
    check(intake['send'](tms_order(state='cancelled')), 202); order_intake.process_pending(intake['org'])
    assert next(o for o in orders(client) if o['id'] == first['id'])['status'] == 'CANCELLED'
    assert inbox(client)[0]['status'] == 'ORDER_CANCELLED'


def test_unmapped_data_waits_for_review_and_can_be_retried_or_dismissed(client, intake):
    check(intake['send'](tms_order(serviceLevel='WHITE GLOVE', lines=[{'qty': 1, 'lbs': 5, 'l': 1, 'w': 1, 'h': 1}])), 202)
    check(intake['send'](tms_order('TMS-2', charges={'total': 50})), 202)
    order_intake.process_pending(intake['org'])
    review = {row['external_id']: row for row in inbox(client, 'open')}
    assert review['TMS-1']['status'] == 'NEEDS_REVIEW' and "Service level 'WHITE GLOVE' does not match a Dispatra service" in review['TMS-1']['missing']
    assert any('stop IDs are needed' in reason for reason in review['TMS-1']['missing'])
    assert review['TMS-2']['missing'] == ['Tax']
    assert orders(client) == []
    check(post(client, BASE + f'/inbox/{review["TMS-2"]["id"]}/dismiss', {}))
    check(post(client, BASE + f'/inbox/{review["TMS-2"]["id"]}/retry', {}), 409)
    # After the catalogue gains a matching service, a retry creates the order.
    services = [c for c in check(client.get('/api/v1/companies/acme/catalog')) if c['kind'] == 'SERVICE']
    template = services[0]
    check(post(client, '/api/v1/companies/acme/catalog', {'kind': 'SERVICE', 'code': 'WHITE_GLOVE', 'data': {**template['data'], 'name': 'White glove'}}), 201)
    check(intake['send'](tms_order(serviceLevel='white glove')), 202)
    order_intake.process_pending(intake['org'])
    assert [o['facts']['external_reference'] for o in orders(client)] == ['TMS-1']
    assert {row['external_id'] for row in inbox(client, 'open')} == {'TMS-1'} or all(r['status'] != 'NEEDS_REVIEW' or r['external_id'] == 'TMS-1' for r in inbox(client))


def test_assigned_orders_are_not_changed_by_the_tms(client, intake):
    check(intake['send'](tms_order()), 202); order_intake.process_pending(intake['org'])
    order = orders(client)[0]
    with owner_engine.begin() as db: db.execute(text("UPDATE orders SET status = 'ASSIGNED' WHERE id = :id"), {'id': order['id']})
    check(intake['send'](tms_order(notes='changed')), 202); order_intake.process_pending(intake['org'])
    assert 'apply TMS changes manually' in inbox(client, 'open')[0]['missing'][0]
    check(intake['send'](tms_order(state='CANCELLED')), 202); order_intake.process_pending(intake['org'])
    assert any('already assigned' in r['missing'][0] for r in inbox(client, 'open'))


def test_polling_stores_orders_from_the_orders_endpoint(client, intake, monkeypatch):
    check(put(client, body(sync_method='polling', credential=None, webhook_secret=None, field_maps={'order': ORDER_MAP}, version=check(client.get(BASE))['version'])))
    monkeypatch.setattr(tms_client, 'request', lambda method, target, auth, body=None: {'orders': [tms_order(), tms_order('TMS-2')]} if target.endswith('/orders') else None)
    assert order_intake.poll(intake['org']) == 2 and order_intake.poll(intake['org']) == 0
    assert order_intake.process_pending(intake['org']) == 2
    assert sorted(o['facts']['external_reference'] for o in orders(client)) == ['TMS-1', 'TMS-2']
    assert check(client.get(BASE))['last_order_poll_at']



# ---- Outbound order events -------------------------------------------------------------------------------------

from app.integrations import outbound  # noqa: E402


def sent_to(calls, fragment):
    return [call for call in calls if fragment in call[1]]


def test_tms_receives_status_for_its_own_orders_in_order_and_retries_failures(client, intake, monkeypatch):
    calls, failing = [], {'on': False}
    def request(method, target, auth, body=None):
        if failing['on']: raise tms_client.TmsError('SERVER_ERROR', 500)
        calls.append((method, target, auth, body))
    monkeypatch.setattr(tms_client, 'request', request)
    check(intake['send'](tms_order()), 202); order_intake.process_pending(intake['org'])
    order = orders(client)[0]
    # A dispatcher's own order never reaches the TMS.
    shipper = order['shipper_id']
    catalog = check(client.get('/api/v1/companies/acme/catalog'))
    service = next(c['id'] for c in catalog if c['kind'] == 'SERVICE')
    stop_ids = [str(stable) for stable in (order_intake.stable(intake['org'], 'x', 1), order_intake.stable(intake['org'], 'x', 2))]
    local = dict(text='1 Main Street, Vancouver, BC V6B 1A1', city='Vancouver', province='BC', postal_code='V6B 1A1', latitude=49.28, longitude=-123.11)
    check(post(client, '/api/v1/companies/acme/orders', dict(shipper_id=shipper, service_id=service, scheduled_at='2026-10-12T09:00:00-07:00',
        stops=[dict(id=stop_ids[0], kind='PICKUP', address=local), dict(id=stop_ids[1], kind='DROPOFF', address=local)],
        items=[dict(id=str(order_intake.stable(intake['org'], 'x', 'i')), pickup_id=stop_ids[0], delivery_id=stop_ids[1], quantity=1, weight_kg='5', length_cm='10', width_cm='10', height_cm='10')])), 201)
    assert outbound.deliver(intake['org'], outbound.TMS_STATUS) == 1
    [(method, target, auth, body_)] = sent_to(calls, '/orders/')
    assert (method, target) == ('POST', 'https://api.tms.example/v1/orders/TMS-1/status')
    assert auth['X-API-Key'] == CREDENTIAL and auth['Idempotency-Key'] == body_['event_id'] and auth['X-Dispatra-Event'] == 'order.created'
    assert body_['event'] == 'order.created' and body_['order'] == {'id': order['id'], 'number': order['number'], 'status': 'NEW', 'reference': 'TMS-1', 'tms_id': 'TMS-1'}
    assert body_['driver'] is None and body_['stop'] is None
    # A failure keeps the position; the same event is sent again on the next run.
    check(post(client, f'/api/v1/companies/acme/orders/{order["id"]}/cancel', {'version': order['version']}))
    failing['on'] = True
    assert outbound.deliver(intake['org'], outbound.TMS_STATUS) == 0
    assert check(client.get(BASE))['last_status_error'] == 'SERVER_ERROR'
    failing['on'] = False
    assert outbound.deliver(intake['org'], outbound.TMS_STATUS) == 1
    assert [call[3]['event'] for call in sent_to(calls, '/orders/')] == ['order.created', 'order.cancelled']
    assert outbound.deliver(intake['org'], outbound.TMS_STATUS) == 0
    assert check(client.get(BASE))['last_status_error'] is None


def test_company_webhook_receives_signed_events_for_every_order(client, intake, monkeypatch):
    calls = []
    monkeypatch.setattr(tms_client, 'request', lambda method, target, auth, body=None: calls.append((method, target, auth, body)))
    hook = '/api/v1/companies/acme/integrations/webhooks'
    assert check(client.get(hook)) is None
    check(client.put(hook, json={'url': 'https://hooks.shipper.example/dispatra'}, headers={'Idempotency-Key': 'hook-save-0001-aaaa'}), 422)
    check(client.put(hook, json={'url': 'https://internal.tms.example/x', 'secret': SECRET}, headers={'Idempotency-Key': 'hook-save-0002-aaaa'}), 422)
    saved = check(client.put(hook, json={'url': 'https://hooks.shipper.example/dispatra', 'secret': SECRET}, headers={'Idempotency-Key': 'hook-save-0003-aaaa'}))
    assert saved['secret_configured'] and 'secret' not in saved
    check(intake['send'](tms_order()), 202); order_intake.process_pending(intake['org'])
    assert outbound.deliver(intake['org'], outbound.API_WEBHOOKS) == 1
    [(method, target, auth, body_)] = sent_to(calls, 'hooks.shipper.example')
    raw = json.dumps(body_, separators=(',', ':')).encode()
    assert target == 'https://hooks.shipper.example/dispatra' and auth['X-Signature'] == signature(raw)
    assert body_['order']['tms_id'] == 'TMS-1' and body_['event'] == 'order.created'
    assert check(client.get(hook))['last_delivery_at']
    # Saving again after a delivery (a stored timestamp) works and is replay-safe.
    again = {'url': 'https://hooks.shipper.example/v2', 'version': saved['version']}
    first = check(client.put(hook, json=again, headers={'Idempotency-Key': 'hook-save-0004-aaaa'}))
    assert first['url'].endswith('/v2') and check(client.put(hook, json=again, headers={'Idempotency-Key': 'hook-save-0004-aaaa'})) == first


# ---- Dispatra API keys -------------------------------------------------------------------------------------------

def test_api_keys_act_as_the_company_on_documented_endpoints_only(client, tms):
    keys = '/api/v1/companies/acme/integrations/api-key'
    assert check(client.get(keys)) == {'configured': False, 'prefix': None, 'created_at': None, 'last_used_at': None}
    created = check(post(client, keys, {}), 201)
    secret = created['key']
    assert secret.startswith('dsp_') and len(secret) == 68 and created['prefix'] == secret[:12]
    with owner_engine.connect() as db:
        stored = db.execute(text('SELECT key_hash FROM api_keys')).scalar()
    assert stored == hashlib.sha256(secret.encode()).hexdigest() and secret not in stored
    api = TestClient(app)  # a server: no session, Origin or X-Requested-With
    auth = {'Authorization': f'Bearer {secret}'}
    assert api.get('/api/v1/companies/acme/orders', headers=auth).status_code == 200
    catalog = api.get('/api/v1/companies/acme/catalog', headers=auth)
    assert catalog.status_code == 200 and catalog.json()
    address = dict(text='1 Main Street, Vancouver, BC V6B 1A1', city='Vancouver', province='BC', postal_code='V6B 1A1')
    shipper = api.post('/api/v1/companies/acme/shippers', headers={**auth, 'Idempotency-Key': 'api-key-shipper-0001'},
        json=dict(name='Api Shipper', kind='BUSINESS', company_name='Api Co', email='api@shipper.example', warehouse=address))
    assert shipper.status_code == 201, shipper.text
    for method, path in [('POST', '/catalog'), ('GET', '/settings'), ('GET', '/integrations/tms'), ('GET', '/rate-cards'), ('POST', '/integrations/api-key')]:
        assert api.request(method, '/api/v1/companies/acme' + path, headers={**auth, 'Idempotency-Key': 'api-key-blocked-0001'}).status_code == 403, path
    assert api.get('/api/v1/companies/other/orders', headers=auth).status_code == 404
    assert api.get('/api/v1/companies/acme/orders', headers={'Authorization': 'Bearer dsp_' + '0' * 64}).status_code == 401
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT actor_type FROM events WHERE type = 'shipper.created' ORDER BY seq DESC LIMIT 1")).scalar() == 'SYSTEM'
    assert check(client.get(keys))['last_used_at']
    # Regenerating revokes the previous key at once.
    replacement = check(post(client, keys, {}), 201)['key']
    assert api.get('/api/v1/companies/acme/orders', headers=auth).status_code == 401
    assert api.get('/api/v1/companies/acme/orders', headers={'Authorization': f'Bearer {replacement}'}).status_code == 200
    api.close()
