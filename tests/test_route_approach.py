"""Real tenant-scoped map approach reads with mocked road providers."""
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.models import now
from app.operations import openrouteservice, tomtom, travel
from conftest import HEADERS, owner_engine, login, company, post
from test_manual_operations import BASE, setup as seeded_setup, new_order, assign, check, finish


@pytest.fixture
def setup(seeded_setup):
    w = seeded_setup
    data = {**w['vehicle']['data'], 'parking_address': {**w['vehicle']['data']['parking_address'], 'latitude': 49.24}}
    w['vehicle'] = check(w['client'].put(BASE + f'/vehicles/{w["vehicle"]["id"]}',
        json=dict(version=w['vehicle']['version'], data=data), headers={'Idempotency-Key': str(uuid4())}))
    return w


def endpoint(route):
    return BASE + f'/routes/{route["id"]}/approach'


def roads(monkeypatch, provider='openrouteservice'):
    calls = []
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', provider)
    def route(addresses, vehicle, **options):
        calls.append((addresses, vehicle, options))
        return {'points': [[a['latitude'], a['longitude']] for a in addresses]}
    monkeypatch.setattr(openrouteservice if provider == 'openrouteservice' else tomtom, 'route', route)
    return calls


@pytest.mark.parametrize('gps', ['fresh', 'absent', 'stale', 'inaccurate', 'ended', 'future'])
def test_approach_uses_fresh_duty_gps_then_parking_without_repricing(setup, monkeypatch, gps):
    w = setup; order = new_order(w); route = assign(w, order)
    if gps != 'absent':
        check(post(w['mobile'], BASE + '/driver/location', dict(duty_id=w['duty']['id'],
            captured_at=now().isoformat(), latitude=49.28, longitude=-123.14,
            accuracy_m=100 if gps == 'inaccurate' else 10, location_permission='GRANTED')), 201)
        with owner_engine.begin() as db:
            if gps in {'stale', 'future'}:
                db.execute(text('UPDATE driver_locations SET captured_at=:time'),
                    {'time': now() + timedelta(minutes=3 if gps == 'future' else -3)})
            if gps == 'ended':
                db.execute(text('UPDATE duty_sessions SET ended_at=:time'), {'time': now()})
    calls = roads(monkeypatch)
    path = check(w['client'].get(endpoint(route)))
    expected = [49.28, -123.14] if gps == 'fresh' else [49.24, -123.11]
    assert path['points'][0] == expected
    assert path['points'][-1] == [route['stops'][0]['stop']['address'][key] for key in ('latitude', 'longitude')]
    assert path['provider'] == 'OPENROUTESERVICE' and path['truck_profile_applied']
    assert calls[0][2]['geometry'] and calls[0][2]['cache_scope']
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}'))['pricing'] == order['pricing']


def test_approach_is_one_next_visit_for_shared_route_and_advances_after_pickup(setup, monkeypatch):
    w = setup; first = new_order(w); route = assign(w, first)
    second = new_order(w); route = assign(w, second, route)
    calls = roads(monkeypatch, 'tomtom')
    check(w['client'].get(endpoint(route)))
    assert len(calls) == 1 and len(calls[0][0]) == 2
    route = check(post(w['mobile'], BASE + f'/driver/routes/{route["id"]}/start',
        dict(version=route['version'], generation=route['generation'])))
    visit = route['stops'][0]
    route = check(post(w['mobile'], BASE + f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',
        dict(version=route['version'], generation=route['generation'], captured_at=now().isoformat())))
    route = check(post(w['mobile'], BASE + f'/driver/routes/{route["id"]}/stops/{visit["id"]}/complete',
        dict(version=route['version'], generation=route['generation'], captured_at=now().isoformat(),
            quantities={item['id']: item['quantity'] for item in route['items'] if item['pickup_id'] == visit['stop_id']})))
    check(w['client'].get(endpoint(route)))
    assert calls[-1][0][-1] == route['stops'][1]['stop']['address']


def test_approach_is_dispatcher_only_and_tenant_scoped(setup, monkeypatch):
    w = setup; order = new_order(w); route = assign(w, order); calls = roads(monkeypatch)
    assert w['mobile'].get(endpoint(route)).status_code == 403
    with TestClient(app, headers=HEADERS) as shipper:
        login(shipper, 'customer', 'acme', 'alice@example.com', w['shipper']['initial_password'])
        assert shipper.get(endpoint(route)).status_code == 403
        own_path = check(shipper.get(BASE + f'/orders/{order["id"]}/road-path'))
        assert len(calls[-1][0]) == len(order['facts']['stops'])
        assert own_path['points'][0] == [49.26, -123.11]
    count = len(calls)
    login(w['client']); company(w['client'], 'other'); login(w['client'], 'dispatch', 'other', 'dispatcher')
    assert w['client'].get(endpoint(route).replace('/acme/', '/other/')).status_code == 404
    assert len(calls) == count


def test_missing_location_and_provider_failure_do_not_fabricate_approach(setup, monkeypatch):
    w = setup; route = assign(w, new_order(w)); calls = roads(monkeypatch)
    with owner_engine.begin() as db:
        db.execute(text("UPDATE vehicles SET data=data-'parking_address' WHERE id=:id"), {'id': UUID(w['vehicle']['id'])})
    assert w['client'].get(endpoint(route)).status_code == 409
    assert not calls
    check(post(w['mobile'], BASE + '/driver/location', dict(duty_id=w['duty']['id'], captured_at=now().isoformat(),
        latitude=49.28, longitude=-123.14, accuracy_m=10, location_permission='GRANTED')), 201)
    def unavailable(*args, **kwargs):
        raise HTTPException(503, 'Road provider unavailable')
    monkeypatch.setattr(openrouteservice, 'route', unavailable)
    assert w['client'].get(endpoint(route)).status_code == 503


@pytest.mark.parametrize('terminal', ['released', 'completed'])
def test_terminal_routes_have_no_approach(setup, monkeypatch, terminal):
    w = setup; route = assign(w, new_order(w))
    if terminal == 'released':
        check(post(w['client'], BASE + f'/routes/{route["id"]}/release', dict(version=route['version'], generation=route['generation'])))
    else:
        finish(w, route)
    calls = roads(monkeypatch)
    assert check(w['client'].get(endpoint(route)))['points'] == []
    assert not calls


def test_google_approach_requests_origin_and_next_stop(setup, monkeypatch):
    w = setup; route = assign(w, new_order(w)); calls = []
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', 'google')
    def google(db, actor, method, payload, mask):
        calls.append(payload)
        return {'routes': [{'polyline': {'encodedPolyline': '_p~iF~ps|U_ulLnnqC_mqNvxq`@'}}]}
    monkeypatch.setattr(travel, 'google', google)
    assert len(check(w['client'].get(endpoint(route)))['points']) == 3
    assert calls[0]['origin'] == travel.waypoint(w['vehicle']['data']['parking_address'])
    assert calls[0]['destination'] == travel.waypoint(route['stops'][0]['stop']['address'])


def test_vehicle_already_at_pickup_needs_no_road_request(setup, monkeypatch):
    w = setup; route = assign(w, new_order(w)); calls = roads(monkeypatch)
    check(post(w['mobile'], BASE + '/driver/location', dict(duty_id=w['duty']['id'], captured_at=now().isoformat(),
        latitude=49.26, longitude=-123.11, accuracy_m=10, location_permission='GRANTED')), 201)
    assert check(w['client'].get(endpoint(route)))['points'] == []
    assert not calls
