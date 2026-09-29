"""Contract checks for the unchanged dispatcher UI's reads and archive actions."""
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from conftest import HEADERS, login, post
from test_manual_operations import setup, booking, check, BASE, new_order


def archive(client, resource, row, key=None):
    return post(client, BASE + f'/{resource}/{row["id"]}/archive',
                {'version': row['version']}, key=key)


def test_order_filters_are_tenant_scoped_and_search_is_literal(setup):
    w = setup
    first_body = booking(w['shipper']['id'], w['service'], w['type_id'])
    first_body['external_reference'] = 'PO-ALPHA'
    first = check(post(w['client'], BASE + '/orders', first_body), 201)
    second_body = booking(w['shipper']['id'], w['service'], w['type_id'])
    second_body['scheduled_at'] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    second = check(post(w['client'], BASE + '/orders', second_body), 201)
    assert first['service_id'] == w['service']
    assert [r['id'] for r in check(w['client'].get(BASE + '/orders', params={'search': 'PO-ALPHA'}))] == [first['id']]
    assert [r['id'] for r in check(w['client'].get(BASE + '/orders', params={'search': first['number']}))] == [first['id']]
    assert len(check(w['client'].get(BASE + '/orders', params={'search': 'Alice Shipper'}))) == 2
    assert check(w['client'].get(BASE + '/orders', params={'search': '%'})) == []
    assert {r['id'] for r in check(w['client'].get(BASE + '/orders', params={'service_id': w['service']}))} == {first['id'], second['id']}
    assert check(w['client'].get(BASE + '/orders', params={'service_id': str(uuid4())})) == []
    assert len(check(w['client'].get(BASE + '/orders', params={'status': 'NEW', 'pricing_status': 'PRICED'}))) == 2
    assert w['client'].get(BASE + '/orders', params={'status': 'made-up'}).status_code == 422
    assert w['client'].get(BASE + '/orders', params={'date_from': second_body['scheduled_at'], 'date_to': first_body['scheduled_at']}).status_code == 422
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        assert len(check(portal.get(BASE + '/orders', params={'search': 'Alice'}))) == 2
        assert portal.get(BASE + '/drivers').status_code == 403


def test_archive_preserves_history_and_rejects_active_dependencies(setup):
    w = setup
    client = w['client']
    order = check(post(client, BASE + '/orders', booking(w['shipper']['id'], w['service'], w['type_id'])), 201)
    assert archive(client, 'shippers', w['shipper']).status_code == 409
    assert archive(client, 'vehicles', w['vehicle']).status_code == 409
    unbound = next(rate for rate in w['rates'] if not rate['is_default'] and rate['id'] != w['fixed'] and rate['active'])
    key = str(uuid4())
    archived = check(archive(client, 'rate-cards', unbound, key))
    assert archived == check(archive(client, 'rate-cards', unbound, key))
    assert archived['version'] == unbound['version'] + 1
    assert client.put(BASE + '/rate-cards/' + unbound['id'], json={
        'version': archived['version'], 'is_default': False, 'active': True,
        'data': unbound['data']}, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    assert check(client.get(BASE + '/rate-cards'))
    assert check(client.get(BASE + f'/orders/{order["id"]}'))['pricing'] == order['pricing']
    assert archive(client, 'rate-cards', next(rate for rate in w['rates'] if rate['is_default'])).status_code == 409
    driver = check(archive(client, 'drivers', w['driver']))
    assert driver['version'] == w['driver']['version'] + 1
    assert check(client.get(BASE + f'/drivers/{w["driver"]["id"]}'))['active'] is False
    assert all(row['id'] != w['driver']['id'] for row in check(client.get(BASE + '/drivers')))
    assert any(row['id'] == w['driver']['id'] for row in check(client.get(BASE + '/drivers', params={'include_archived': 'true'})))
    assert client.put(BASE + '/drivers/' + w['driver']['id'], json={'version': driver['version'], 'data': w['driver_body']}, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    vehicle = check(archive(client, 'vehicles', w['vehicle']))
    assert vehicle['version'] == w['vehicle']['version'] + 1
    assert check(client.get(BASE + f'/vehicles/{w["vehicle"]["id"]}'))['active'] is False
    assert all(row['id'] != w['vehicle']['id'] for row in check(client.get(BASE + '/vehicles')))
    # The active driver session was revoked, including its open duty session.
    assert w['mobile'].get(BASE + '/driver/profile').status_code == 401


def test_archive_shipper_revokes_login_and_keeps_profile(setup):
    w = setup
    result = check(archive(w['client'], 'shippers', w['shipper']))
    assert result['version'] == w['shipper']['version'] + 1
    saved = check(w['client'].get(BASE + f'/shippers/{w["shipper"]["id"]}'))
    assert saved['status'] == 'INACTIVE' and saved['archived_at']
    assert all(row['id'] != w['shipper']['id'] for row in check(w['client'].get(BASE + '/shippers')))
    assert any(row['id'] == w['shipper']['id'] for row in check(w['client'].get(BASE + '/shippers', params={'include_archived': 'true'})))
    with TestClient(app, headers=HEADERS) as portal:
        response = portal.post('/api/v1/auth/login', json={
            'portal': 'customer', 'organization': 'acme',
            'login_id': w['shipper']['email'], 'password': w['shipper']['initial_password']})
        assert response.status_code == 401


def test_historical_shipper_payment_terms_remain_editable(setup):
    w = setup
    for term in ('NET7', 'NET60'):
        body = {**w['shipper_body'], 'email': f'{term.lower()}@example.com', 'terms': term}
        saved = check(post(w['client'], BASE + '/shippers', body), 201)
        assert saved['terms'] == term
        assert check(w['client'].get(BASE + f'/shippers/{saved["id"]}'))['terms'] == term


def test_monitor_derives_attention_without_changing_order_status(setup):
    w = setup
    body = booking(w['shipper']['id'], w['service'], w['type_id'])
    body['scheduled_at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    body['stops'][1]['window_end'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    order = check(post(w['client'], BASE + '/orders', body), 201)
    monitor = check(w['client'].get(BASE + '/monitor'))
    projected = next(item for item in monitor['orders'] if item['id'] == order['id'])
    assert projected['status'] == 'NEW'
    assert {'LATE_START', 'AT_RISK'}.issubset(projected['attention_flags'])
    assert {item['kind'] for item in monitor['needs_attention'] if item['order_id'] == order['id']} >= {'LATE_START', 'AT_RISK'}
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}'))['status'] == 'NEW'


def test_public_entity_numbers_are_company_scoped_and_server_owned(setup, monkeypatch):
    w = setup
    assert re.fullmatch(r'DAD-[1-9][0-9]{3}', w['driver']['number'])
    assert re.fullmatch(r'DAS-[1-9][0-9]{3}', w['shipper']['number'])
    assert re.fullmatch(r'DAV-[1-9][0-9]{3}', w['vehicle']['number'])
    order = new_order(w)
    assert re.fullmatch(r'DAO-[1-9][0-9]{3}', order['number'])
    data = {**w['driver_body'], 'name': 'Second Driver', 'email': 'second-driver@example.com', 'vehicle_id': None}
    second = check(post(w['client'], BASE + '/drivers', data), 201)
    assert re.fullmatch(r'DAD-[1-9][0-9]{3}', second['number'])
    assert second['number'] != w['driver']['number']
    assert check(w['client'].get(BASE + f'/drivers/{second["id"]}'))['number'] == second['number']
    from app.operations.identifiers import unused_number
    monkeypatch.setattr('app.operations.identifiers.secrets.randbelow', lambda count: 0)
    assert unused_number('DAD-', {'DAD-1000'}) != 'DAD-1000'
    assert unused_number('DAD-', {f'DAD-{n}' for n in range(1000, 10000)}) == 'DAD-10000'
