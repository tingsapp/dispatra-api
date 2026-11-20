from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.operations import dispatch, driver_route_path


def test_planned_route_map_origin_prefers_fresh_gps(monkeypatch):
    gps = {'latitude': 49.25, 'longitude': -123.12}
    route = SimpleNamespace(status='PLANNED', driver_id='driver')
    vehicle = SimpleNamespace(data={'parking_address': {'latitude': 49.26, 'longitude': -123.11}})
    driver = SimpleNamespace(id='driver')
    from app.dispatch import candidates
    monkeypatch.setattr(dispatch, 'record', lambda *_: driver)
    monkeypatch.setattr(candidates, 'position', lambda *_: (gps, 'GPS'))

    assert dispatch.route_map_origin(None, None, route, vehicle) == (gps, 'GPS')


def test_planned_route_map_origin_falls_back_to_parking(monkeypatch):
    parking = {'latitude': 49.26, 'longitude': -123.11}
    route = SimpleNamespace(status='PLANNED', driver_id='driver')
    vehicle = SimpleNamespace(data={'parking_address': parking})
    from app.dispatch import candidates
    monkeypatch.setattr(dispatch, 'record', lambda *_: SimpleNamespace(id='driver'))
    monkeypatch.setattr(candidates, 'position', lambda *_: (parking, 'PARKING'))

    assert dispatch.route_map_origin(None, None, route, vehicle) == (parking, 'PARKING')


def test_planned_route_without_gps_or_parking_has_no_map_origin(monkeypatch):
    route = SimpleNamespace(status='PLANNED', driver_id='driver')
    vehicle = SimpleNamespace(data={'parking_address': {'latitude': None, 'longitude': None}})
    from app.dispatch import candidates
    monkeypatch.setattr(dispatch, 'record', lambda *_: SimpleNamespace(id='driver'))
    monkeypatch.setattr(candidates, 'position', lambda *_: (None, None))

    assert dispatch.route_map_origin(None, None, route, vehicle) == (None, None)


@pytest.mark.parametrize('provider', ['google', 'demo', 'disabled'])
def test_driver_route_geometry_rejects_providers_without_truck_routing(provider):
    with pytest.raises(HTTPException, match='Truck road routing is not configured'):
        driver_route_path.require_truck_provider(provider)


@pytest.mark.parametrize('provider', ['tomtom', 'openrouteservice'])
def test_driver_route_geometry_accepts_truck_routing_providers(provider):
    assert driver_route_path.require_truck_provider(provider) == provider
