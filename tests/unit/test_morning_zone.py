"""Morning zone pass: today's pickups booked before opening and priced with a Zone Rate Card go to vehicles parked in that zone first."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from app.dispatch import service
from app.dispatch.geography import morning_zone
from app.dispatch.policy import ordered, prefer_proposal
from app.operations.calendar import day_bounds, opening
from app.operations.schemas import RateData, SettingsData

VANCOUVER = ZoneInfo('America/Vancouver')
CONFIG = SettingsData()
NOW = datetime(2030, 1, 2, 10, tzinfo=VANCOUVER)
ZONES = RateData(name='Zones', method='ZONE', zones=[{'id': 'n', 'name': 'North', 'postal_codes': ['V7L']},
    {'id': 's', 'name': 'South', 'postal_codes': ['V5X']}, {'id': 'sea', 'name': 'Seattle', 'postal_codes': ['981']}, {'id': 'dt', 'name': 'Downtown Seattle', 'postal_codes': ['98101']}],
    weight_bands=[{'from_kg': 0, 'to_kg': 99, 'prices': {'n': 20, 's': 20, 'sea': 25, 'dt': 30}}]).model_dump(mode='json')


def local(day, hour):
    return datetime(2030, 1, day, hour, tzinfo=VANCOUVER)


def order(created, scheduled, postal='V7L 1A1', card=ZONES):
    stop = lambda kind, code: {'kind': kind, 'address': {'postal_code': code, 'latitude': 49.3, 'longitude': -123.1}}
    return SimpleNamespace(created_at=created, scheduled_at=scheduled, pricing={'context': {'card': card}},
        facts={'stops': [stop('PICKUP', 'V6A 1E9'), stop('DROPOFF', postal)]})


def candidate(identity, extra, score, orders=0, zone=None):
    return dict(driver_id=identity, driver_number=identity, score=score, facts=['feasible'],
                metrics=dict(extra_minutes=extra, route_orders=orders, home_zone_match=zone))


def test_opening_and_day_bounds_use_company_time_zone():
    assert opening(CONFIG, NOW) == local(2, 6)
    assert day_bounds(CONFIG, NOW) == (local(2, 0), local(3, 0))


FIXED = RateData(name='Flat', method='FIXED').model_dump(mode='json')


@pytest.mark.parametrize('created,scheduled,postal,card,expected', [
    (local(1, 15), local(2, 9), 'V7L 1A1', ZONES, 'North'),   # booked days before, picked up today
    (local(2, 5), local(2, 14), 'V5X 2B2', ZONES, 'South'),   # booked before opening today
    (local(1, 8), local(1, 9), 'V7L 1A1', ZONES, 'North'),    # overdue from yesterday
    (local(2, 7), local(2, 9), 'V7L 1A1', ZONES, None),       # booked after opening: distance rules
    (local(1, 15), local(3, 9), 'V7L 1A1', ZONES, None),      # tomorrow's pickup waits for its own day
    (local(1, 15), local(2, 9), 'V6A 1E9', ZONES, None),      # delivery outside every card zone
    (local(1, 15), local(2, 9), 'V7L 1A1', FIXED, None),      # priced by fixed amount: distance rules
    (local(1, 15), local(2, 9), 'V7L 1A1', None, None),       # no pricing card recorded
])
def test_morning_zone_comes_from_the_orders_zone_rate_card(created, scheduled, postal, card, expected):
    home = morning_zone(order(created, scheduled, postal, card), CONFIG, NOW)
    assert (home.name if home else None) == expected


def test_vehicle_parking_uses_the_same_card_and_most_specific_prefix():
    home = morning_zone(order(local(1, 15), local(2, 9), '98101'), CONFIG, NOW)
    assert home.name == 'Downtown Seattle'
    assert home.contains({'postal_code': '98101-1234'}) and not home.contains({'postal_code': '98109'})
    north = morning_zone(order(local(1, 15), local(2, 9)), CONFIG, NOW)
    assert north.contains({'postal_code': 'v7l 3b3'}) and not north.contains({'postal_code': 'V5X 1A1'})
    assert not north.contains(None) and not north.contains({})


def test_far_home_zone_vehicle_beats_near_outside_vehicle_and_its_consolidation():
    far = candidate('far', 55, 70, zone=True)
    near = candidate('near', 3, 5, zone=False)
    merge = candidate('merge', 0, 2, orders=1, zone=False)
    assert [c['driver_id'] for c in ordered([near, merge, far])] == ['far', 'merge', 'near']


def test_consolidation_still_orders_drivers_inside_the_zone():
    idle = candidate('idle', 9, 10, zone=True)
    merge = candidate('merge', 2, 25, orders=1, zone=True)
    assert ordered([idle, merge])[0] is merge


def test_no_zone_rule_keeps_location_ranking():
    far, near = candidate('far', 55, 70), candidate('near', 3, 5)
    assert ordered([far, near])[0] is near


def test_fleet_proposal_and_ai_cannot_move_order_outside_the_zone(monkeypatch):
    inside, outside = candidate('inside', 40, 50, zone=True), candidate('outside', 2, 4, zone=False)
    assert prefer_proposal([inside, outside], 'outside')[0] is inside
    monkeypatch.setattr(service.rank, 'rank', lambda *args: ([outside, inside], 'outside first'))
    assert service.ranked({}, [inside, outside])[0][0] is inside
