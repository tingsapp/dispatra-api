"""Dispatch policy: measured route consolidation survives score and proposal overrides."""
import pytest
from app.dispatch.policy import ordered, priority, prefer_proposal
from app.dispatch import service


def candidate(identity, extra, score, orders=0, **metrics):
    return dict(driver_id=identity, driver_number=identity, score=score, facts=['feasible'],
                metrics=dict(extra_minutes=extra, route_orders=orders, **metrics))


def test_same_path_consolidation_beats_idle_driver_despite_workload():
    merge=candidate('Alex',0.1,36.49,1);idle=candidate('Jordan',8.6,10.83)
    assert ordered([idle,merge])[0] is merge
    assert prefer_proposal([merge,idle],'Jordan')[0] is merge


@pytest.mark.parametrize('metrics',[{}, {'live_insertion':True}, {'zone_match':False}])
def test_priority_uses_feasible_added_driving_not_area_label(metrics):
    merge=candidate('merge',2,25,1,**metrics);idle=candidate('idle',9,10)
    assert priority(merge,[idle,merge])==0


def test_large_route_detour_does_not_gain_consolidation_priority():
    merge=candidate('merge',20,30,1);idle=candidate('idle',9,10)
    assert ordered([merge,idle])[0] is idle


def test_expected_driver_does_not_displace_confirmed_consolidation():
    expected=candidate('expected',0,0,1,awaiting_duty=True)
    merge=candidate('merge',1,30,1)
    assert ordered([expected,merge])[0] is merge


def test_ai_cannot_override_consolidation(monkeypatch):
    merge=candidate('merge',0.1,36,1);idle=candidate('idle',8.6,10)
    monkeypatch.setattr(service.rank,'rank',lambda *args:([idle,merge],'idle first'))
    assert service.ranked({},[merge,idle])[0][0] is merge


def test_rule_fallback_preserves_consolidation(monkeypatch):
    merge=candidate('merge',0.1,36,1);idle=candidate('idle',8.6,10)
    def unavailable(*args):raise service.rank.Unavailable('AI_NOT_CONFIGURED')
    monkeypatch.setattr(service.rank,'rank',unavailable)
    assert service.ranked({},[idle,merge])[0][0]['driver_id']=='merge'


def test_long_idle_wait_between_windows_does_not_beat_idle_driver():
    xia = candidate('Xia', 32.9, 126.5, 1, extra_wait_minutes=85.6)
    driver = candidate('Driver', 46.5, 50.6, 0, extra_wait_minutes=0.0)
    assert priority(xia, [xia, driver]) == 1
    assert ordered([xia, driver])[0] is driver


def test_small_wait_consolidation_still_beats_idle_driver():
    xia = candidate('Xia', 20.0, 42.0, 1, extra_wait_minutes=10.0)
    driver = candidate('Driver', 46.5, 50.6, 0, extra_wait_minutes=0.0)
    assert priority(xia, [xia, driver]) == 0
    assert ordered([driver, xia])[0] is xia


def test_wait_minutes_measures_idle_gap_between_stops():
    from types import SimpleNamespace
    from app.dispatch.candidates import wait_minutes
    orders = [
        SimpleNamespace(facts={'stops': [{'id': 'p1', 'service_minutes': 10}, {'id': 'd1', 'service_minutes': 10}]}),
        SimpleNamespace(facts={'stops': [{'id': 'p2', 'service_minutes': 10}, {'id': 'd2', 'service_minutes': 10}]}),
    ]
    tight = {'planned_at': '2026-11-27T11:00:00-07:00', 'travel_minutes': 50.0,
        'stops': [{'stop_id': 'p1', 'planned_at': '2026-11-27T11:05:00-07:00'}, {'stop_id': 'd2', 'planned_at': '2026-11-27T12:00:00-07:00'}]}
    gapped = {'planned_at': '2026-11-27T11:00:00-07:00', 'travel_minutes': 51.9,
        'stops': [
            {'stop_id': 'p1', 'planned_at': '2026-11-27T11:05:00-07:00'},
            {'stop_id': 'p2', 'planned_at': '2026-11-27T13:00:00-07:00'},
            {'stop_id': 'd1', 'planned_at': '2026-11-27T13:37:00-07:00'},
            {'stop_id': 'd2', 'planned_at': '2026-11-27T13:47:00-07:00'},
        ]}
    assert wait_minutes(tight, orders) == 0.0
    assert round(wait_minutes(gapped, orders), 1) == 85.1
