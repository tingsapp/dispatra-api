from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from app.operations.pricing import planned_stop_minutes
from app.operations.schemas import Booking, SettingsData
from app.order_watch.detector import evaluate

AT = datetime(2026, 11, 25, 17, tzinfo=timezone.utc)
ADDRESS = {'text': '123 Main Street, Vancouver', 'city': 'Vancouver', 'province': 'BC', 'postal_code': 'V6A 2S5'}


def booking(load_size, pickup_minutes=None):
    pickup, drop = str(uuid4()), str(uuid4())
    stops = [{'id': pickup, 'kind': 'PICKUP', 'address': ADDRESS, **({'service_minutes': pickup_minutes} if pickup_minutes is not None else {})},
             {'id': drop, 'kind': 'DROPOFF', 'address': ADDRESS}]
    item = {'id': str(uuid4()), 'pickup_id': pickup, 'delivery_id': drop, 'quantity': 1, 'weight_kg': '5', 'length_cm': '10', 'width_cm': '10', 'height_cm': '10'}
    return Booking.model_validate({'load_size': load_size, 'service_id': str(uuid4()), 'vehicle_type_id': str(uuid4()), 'scheduled_at': AT.isoformat(), 'stops': stops, 'items': [item]})


def test_omitted_stop_times_follow_the_company_time_for_the_load_size_and_stated_times_stay():
    config = SettingsData(stop_minutes_small_parcel=8, stop_minutes_ltl=25, stop_minutes_ftl=50)
    with patch('app.operations.pricing.settings', return_value=(None, config)):
        assert [s.service_minutes for s in planned_stop_minutes(None, None, booking('LTL')).stops] == [25, 25]
        assert [s.service_minutes for s in planned_stop_minutes(None, None, booking('FTL', 5)).stops] == [5, 50]
        again = planned_stop_minutes(None, None, planned_stop_minutes(None, None, booking('SMALL_PARCEL')))
        assert [s.service_minutes for s in again.stops] == [8, 8]


def test_default_planned_stop_times():
    config = SettingsData()
    assert (config.stop_minutes_small_parcel, config.stop_minutes_ltl, config.stop_minutes_ftl) == (10, 20, 45)


def watch(minutes_at_stop, rule=None, method='DISTANCE'):
    stop_id, visit_id = uuid4(), uuid4()
    settings = {'extra_time_minutes': 30, 'extra_time_amount': '50'} if rule is None else rule
    order = SimpleNamespace(id=uuid4(), status='IN_PROGRESS', route_id=uuid4(), scheduled_at=AT - timedelta(hours=2),
        facts={'stops': [{'id': str(stop_id), 'kind': 'PICKUP'}]}, pricing={'status': 'PRICED', 'method': method, 'context': {'settings': settings}})
    route = SimpleNamespace(id=order.route_id, status='IN_PROGRESS', planned_at=AT - timedelta(hours=2), generation=1)
    visit = SimpleNamespace(id=visit_id, stop_id=stop_id, status='PENDING', arrived_at=AT - timedelta(minutes=minutes_at_stop))
    stop = SimpleNamespace(id=stop_id, order_id=order.id, kind='PICKUP', data={'service_minutes': 20})
    return [s['kind'] for s in evaluate(order, route, [visit], {stop_id: stop}, None, AT)], evaluate(order, route, [visit], {stop_id: stop}, None, AT)


def test_dispatch_hears_when_free_time_ends_once_per_visit():
    assert watch(25)[0] == ['STOP_OVERRUN']
    kinds, signals = watch(31)
    assert kinds == ['STOP_OVERRUN', 'EXTRA_TIME_STARTED']
    started = signals[1]
    assert started['description'] == 'Free time (30 min) ended at the pickup; Extra time now applies.'
    assert started['certainty'] == 'OBSERVED' and started['fingerprint'].startswith('extra-time:')


def test_no_extra_time_signal_when_the_order_is_not_charged():
    assert watch(90, {'extra_time_minutes': 30, 'extra_time_amount': '0'})[0] == ['STOP_OVERRUN']
    assert watch(90, {})[0] == ['STOP_OVERRUN']
    assert watch(90, method='IMPORTED')[0] == ['STOP_OVERRUN']
