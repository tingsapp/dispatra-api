from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from app.operations import pricing
from app.operations.schemas import Booking, CatalogData, RateData, SettingsData

ADDRESS = {'text': '123 Main Street, Vancouver', 'city': 'Vancouver', 'province': 'BC', 'postal_code': 'V6A 2S5'}
LIFTGATES = {code: SimpleNamespace(id=uuid4(), code=code, data=CatalogData(name=name, amount=25, required_equipment=['LIFTGATE']).model_dump(mode='json'))
    for code, name in [('LIFTGATE', 'Power Liftgate at Pickup'), ('LIFTGATE_DROPOFF', 'Power Liftgate at Drop-off')]}
CARD = SimpleNamespace(id=uuid4(), version=1, active=True, data=RateData(name='Fixed', method='FIXED', fixed_amount=100).model_dump(mode='json'))


class Database:
    def __init__(self, catalogue=LIFTGATES): self.catalogue = catalogue
    def scalar(self, statement): return next((self.catalogue[value] for value in statement.compile().params.values() if value in self.catalogue), None)


def booking(pickup_liftgate, drop_liftgate, accessorials=()):
    pickup, drop = str(uuid4()), str(uuid4())
    stops = [{'id': pickup, 'kind': 'PICKUP', 'address': ADDRESS, 'liftgate': pickup_liftgate}, {'id': drop, 'kind': 'DROPOFF', 'address': ADDRESS, 'liftgate': drop_liftgate}]
    item = {'id': str(uuid4()), 'pickup_id': pickup, 'delivery_id': drop, 'quantity': 1, 'weight_kg': '5', 'length_cm': '10', 'width_cm': '10', 'height_cm': '10'}
    return Booking.model_validate({'service_id': str(uuid4()), 'rate_card_id': str(CARD.id), 'scheduled_at': datetime(2026, 11, 25, 17, tzinfo=timezone.utc).isoformat(),
        'stops': stops, 'items': [item], 'accessorials': list(accessorials)})


def codes(data, db=None):
    by_id = {item.id: item for item in LIFTGATES.values()}
    with patch.object(pricing, 'settings', return_value=(None, SettingsData(fuel_enabled=False, gst_enabled=False))), \
         patch.object(pricing, 'record', return_value=CARD), \
         patch.object(pricing, 'catalog', side_effect=lambda _db, _actor, _id, kind: by_id[_id] if kind == 'ACCESSORIAL' else SimpleNamespace(data=CatalogData(name='Standard').model_dump(mode='json'))):
        context = pricing.price_context(db or Database(), SimpleNamespace(organization_id=uuid4()), data)
    return [(item['code'], item['quantity'], item.get('required_equipment')) for item in context['accessorials']]


def test_pickup_and_drop_off_liftgates_add_their_own_accessorial_once_with_its_equipment():
    assert codes(booking(True, True)) == [('LIFTGATE', '1', ['LIFTGATE']), ('LIFTGATE_DROPOFF', '1', ['LIFTGATE'])]
    assert codes(booking(True, False)) == [('LIFTGATE', '1', ['LIFTGATE'])]
    assert codes(booking(False, True)) == [('LIFTGATE_DROPOFF', '1', ['LIFTGATE'])]
    assert codes(booking(True, False, [{'id': str(LIFTGATES['LIFTGATE'].id), 'quantity': 1}])) == [('LIFTGATE', '1', ['LIFTGATE'])]


def test_no_liftgate_stop_keeps_only_a_hand_picked_liftgate():
    assert codes(booking(False, False)) == []
    assert codes(booking(False, False, [{'id': str(LIFTGATES['LIFTGATE_DROPOFF'].id), 'quantity': 1}])) == [('LIFTGATE_DROPOFF', '1', ['LIFTGATE'])]


def test_a_company_without_liftgate_accessorials_still_books_liftgate_stops():
    data = booking(True, True)
    assert data.stops[1].liftgate and codes(data, Database({})) == []
