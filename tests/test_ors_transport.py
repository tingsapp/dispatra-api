"""Truck restrictions and development Matrix-plus-confirm routing, with no external requests."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.operations import openrouteservice as ors, travel

VEHICLE = {'payload_kg': 1000, 'road_profile': {'length_m': 6, 'width_m': 2, 'height_m': 3, 'gross_weight_kg': 5000, 'axle_weight_kg': 2500}}
POINTS = [{'latitude': 49.28, 'longitude': -123.12}, {'latitude': 49.27, 'longitude': -123.1}]


def reply():
    return {'features': [{'properties': {'segments': [{'duration': 120, 'distance': 1000}]},
                          'geometry': {'coordinates': [[-123.12, 49.28], [-123.1, 49.27]]}}]}


def test_route_sends_hgv_restrictions_and_converts_units(monkeypatch):
    calls = []
    monkeypatch.setattr(ors, 'request', lambda payload: calls.append(payload) or reply())
    route = ors.route(POINTS, VEHICLE, geometry=True)
    assert calls[0]['options']['vehicle_type'] == 'hgv'
    assert calls[0]['options']['profile_params']['restrictions'] == dict(length=6, width=2, height=3, weight=5, axleload=2.5)
    assert calls[0]['coordinates'] == [[-123.12, 49.28], [-123.1, 49.27]]
    assert route['points'] == [[49.28, -123.12], [49.27, -123.1]]
    assert route['legs'][0]['seconds'] == 120


def test_missing_profile_and_dangerous_goods_fail_before_transport(monkeypatch):
    monkeypatch.setattr(ors, 'request', lambda _: pytest.fail('Must validate first'))
    for vehicle, hazardous in [({}, False), (VEHICLE, True)]:
        with pytest.raises(HTTPException) as error:
            ors.route(POINTS, vehicle, hazardous=hazardous)
        assert error.value.status_code == 409


def matrix_reply(payload):
    size = len(payload['locations'])
    return {'durations': [[0 if a == b else 60 * (abs(a - b) + 1) for b in range(size)] for a in range(size)]}


def test_matrix_uses_one_request_for_all_locations_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(ors, 'request', lambda payload, url=ors.BASE: calls.append((payload, url)) or matrix_reply(payload))
    points = [{'latitude': 49.2 + i / 100, 'longitude': -123.1} for i in range(12)]
    stops = [{'id': str(i), 'address': p} for i, p in enumerate(points)] + [{'id': 'same', 'address': points[0]}]
    db = SimpleNamespace(info={})
    result = ors.matrix(db, stops, VEHICLE)
    assert len(calls) == 1 and calls[0][1] == ors.MATRIX
    assert len(calls[0][0]['locations']) == 12 and 'options' not in calls[0][0]
    assert result[('0', '1')] == 2 and result[('3', '0')] == 4
    assert result[('0', 'same')] == 0 and result[('same', '1')] == 2
    ors.matrix(db, stops, VEHICLE)
    assert len(calls) == 1
    with pytest.raises(HTTPException) as error:
        ors.matrix(db, [{'id': str(i), 'address': {'latitude': 49 + i / 1000, 'longitude': -123}} for i in range(51)], VEHICLE)
    assert error.value.status_code == 409 and len(calls) == 1


def test_matrix_skips_unreachable_pairs_and_rejects_malformed_rows(monkeypatch):
    stops = [{'id': str(i), 'address': p} for i, p in enumerate(POINTS)]
    monkeypatch.setattr(ors, 'request', lambda payload, url=ors.BASE: {'durations': [[0, None], [90, 0]]})
    result = ors.matrix(None, stops, VEHICLE)
    assert ('0', '1') not in result and result[('1', '0')] == 1.5
    monkeypatch.setattr(ors, 'request', lambda payload, url=ors.BASE: {'durations': [[0, 1]]})
    with pytest.raises(HTTPException) as error:
        ors.matrix(None, stops, VEHICLE)
    assert error.value.status_code == 503
    monkeypatch.setattr(ors, 'request', lambda *_: pytest.fail('Must validate first'))
    with pytest.raises(HTTPException):
        ors.matrix(None, stops, {})


def test_confirm_sends_one_restricted_route_over_the_planned_order(monkeypatch):
    calls = []
    def reply_legs(payload):
        calls.append(payload)
        legs = [{'duration': 60 * (i + 1), 'distance': 1000} for i in range(len(payload['coordinates']) - 1)]
        return {'features': [{'properties': {'segments': legs}, 'geometry': {'coordinates': payload['coordinates']}}]}
    monkeypatch.setattr(ors, 'request', reply_legs)
    a, b = {'latitude': 49.1, 'longitude': -123.1}, {'latitude': 49.2, 'longitude': -123.2}
    stops = [{'id': 'p', 'address': a}, {'id': 'p2', 'address': a}, {'id': 'd', 'address': b}]
    schedule = [{'stop_id': 'p'}, {'stop_id': 'p2'}, {'stop_id': 'd'}]
    table = ors.confirm(stops, schedule, VEHICLE, origin={'latitude': 49.0, 'longitude': -123.0, 'heading': 90})
    assert len(calls) == 1 and len(calls[0]['coordinates']) == 3
    assert calls[0]['options']['profile_params']['restrictions']['height'] == 3
    assert calls[0]['bearings'][0] == [90, 45]
    assert table == {('__driver__', 'p'): 1, ('p', 'p2'): 0, ('p2', 'd'): 2}


def test_more_than_six_stops_are_planned_and_retimed_with_truck_legs(monkeypatch):
    from datetime import datetime
    from app.operations.routing import optimize
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', 'openrouteservice')
    vehicle = {**VEHICLE, 'payload_kg': 1000, 'volume_m3': 10, 'length_cm': 300, 'width_cm': 200, 'height_cm': 200, 'maximum_stops': 20}
    stops = [{'id': f's{i}', 'kind': 'PICKUP' if i < 5 else 'DELIVERY', 'address': {'latitude': 49 + i / 100, 'longitude': -123}} for i in range(10)]
    items = [{'id': f'i{i}', 'pickup_id': f's{i}', 'delivery_id': f's{i + 5}', 'quantity': 1, 'weight_kg': 10,
              'length_cm': 50, 'width_cm': 50, 'height_cm': 50, 'pallets': 0} for i in range(5)]
    origin = {'latitude': 48.99, 'longitude': -123}
    requests = []
    def provider(payload, url=ors.BASE):
        requests.append(url)
        if url == ors.MATRIX: return matrix_reply(payload)
        legs = [{'duration': 600, 'distance': 1000} for _ in payload['coordinates'][1:]]
        return {'features': [{'properties': {'segments': legs}, 'geometry': {'coordinates': payload['coordinates']}}]}
    monkeypatch.setattr(ors, 'request', provider)
    located = [{'id': '__driver__', 'address': origin}] + stops
    table = travel.matrix(None, None, located, vehicle=vehicle)
    minutes = {s['id']: table.get(('__driver__', s['id'])) for s in stops}
    start = datetime(2026, 11, 13, 8)
    replan = lambda travel_table, origin_minutes: optimize(stops, items, vehicle, start, None, 480, travel_table, origin=origin, origin_minutes=origin_minutes)
    plan = travel.confirm(None, None, stops, replan(table, minutes), replan, vehicle=vehicle, origin=origin)
    assert requests == [ors.MATRIX, ors.BASE]
    assert len(plan['stops']) == 10 and plan['travel_minutes'] == 100
    assert plan['stops'][-1]['planned_at'] == datetime(2026, 11, 13, 9, 40).isoformat()


def test_confirm_explains_when_truck_legs_miss_a_window(monkeypatch):
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', 'openrouteservice')
    monkeypatch.setattr(ors, 'confirm', lambda *a, **kw: {})
    def replan(*_):
        raise HTTPException(409, 'No feasible Route found within the planning limit; review capacity, windows and shift.')
    with pytest.raises(HTTPException) as error:
        travel.confirm(None, None, [], {'stops': []}, replan)
    assert 'size and weight limits' in error.value.detail
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', 'tomtom')
    assert travel.confirm(None, None, [], {'stops': []}, replan) == {'stops': []}


def test_dispatch_provider_selects_ors(monkeypatch):
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER', 'openrouteservice')
    monkeypatch.setattr(ors, 'matrix', lambda *a, **kw: {'truck': True})
    assert travel.matrix(None, None, [], vehicle=VEHICLE) == {'truck': True}


def test_partial_route_is_rejected(monkeypatch):
    monkeypatch.setattr(ors, 'request', lambda _: {'features': []})
    with pytest.raises(HTTPException) as error:
        ors.route(POINTS, VEHICLE)
    assert error.value.status_code == 503


def test_single_leg_summary_without_instructions_is_supported(monkeypatch):
    data = reply()
    data['features'][0]['properties'] = {'summary': {'duration': 120, 'distance': 1000}}
    monkeypatch.setattr(ors, 'request', lambda payload: data)
    assert ors.route(POINTS, VEHICLE)['legs'] == [{'seconds': 120, 'metres': 1000}]
