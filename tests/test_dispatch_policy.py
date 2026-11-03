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
