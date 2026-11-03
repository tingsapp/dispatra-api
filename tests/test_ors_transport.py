"""Truck restrictions and bounded development routing, with no external requests."""
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


def test_matrix_uses_restricted_directions_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(ors, 'request', lambda payload: calls.append(payload) or reply())
    stops = [{'id': str(i), 'address': p} for i, p in enumerate(POINTS)]
    db = SimpleNamespace(info={})
    result = ors.matrix(db, stops, VEHICLE)
    assert result[('0', '1')] == 2 and result[('1', '0')] == 2
    assert result[('0', '0')] == 0
    ors.matrix(db, stops, VEHICLE)
    assert len(calls) == 2
    assert all(c['options']['profile_params']['restrictions']['height'] == 3 for c in calls)
    with pytest.raises(HTTPException):
        ors.matrix(db, stops * 4, VEHICLE)


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
