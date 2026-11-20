from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.intake import geocode
from app.operations.common import located
from app.operations.orders import located_stops
from app.operations.schemas import Address, Booking


def address(lat=None, lng=None):
    return Address(text='43 Powell St, Vancouver, BC V6A 1E9', city='Vancouver', province='BC', postal_code='V6A 1E9', latitude=lat, longitude=lng)


def booking(pickup, drop):
    pickup_id, drop_id = uuid4(), uuid4()
    return Booking.model_validate({'service_id': str(uuid4()), 'scheduled_at': '2030-01-02T18:00:00Z',
        'stops': [{'id': str(pickup_id), 'kind': 'PICKUP', 'address': pickup.model_dump()}, {'id': str(drop_id), 'kind': 'DROPOFF', 'address': drop.model_dump()}],
        'items': [{'id': str(uuid4()), 'pickup_id': str(pickup_id), 'delivery_id': str(drop_id), 'quantity': 1, 'weight_kg': 5, 'length_cm': 30, 'width_cm': 30, 'height_cm': 30}]})


def test_an_address_with_coordinates_is_kept_without_geocoding(monkeypatch):
    monkeypatch.setattr(geocode, 'verify', lambda *_: pytest.fail('geocoded a located address'))
    assert located(address(49.28, -123.1), 'warehouse address').latitude == 49.28


def test_a_missing_map_point_is_geocoded_or_refused_naming_the_address(monkeypatch):
    monkeypatch.setattr(geocode, 'verify', lambda *_: {'latitude': 49.2833, 'longitude': -123.104})
    assert (located(address(), 'warehouse address').latitude, located(address(), 'warehouse address').longitude) == (49.2833, -123.104)
    monkeypatch.setattr(geocode, 'verify', lambda *_: None)
    with pytest.raises(HTTPException) as refused: located(address(), 'warehouse address')
    assert refused.value.status_code == 422 and 'warehouse address' in refused.value.detail and 'suggestions' in refused.value.detail


def test_order_stops_without_coordinates_are_located_or_the_order_is_refused(monkeypatch):
    monkeypatch.setattr(geocode, 'verify', lambda *_: None)
    with pytest.raises(HTTPException) as refused: located_stops(booking(address(), address(49.28, -123.12)))
    assert 'pickup address' in refused.value.detail
    monkeypatch.setattr(geocode, 'verify', lambda *_: {'latitude': 49.2833, 'longitude': -123.104})
    stops = located_stops(booking(address(), address(49.28, -123.12))).stops
    assert [stop.address.latitude for stop in stops] == [49.2833, 49.28]
