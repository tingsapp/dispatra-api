from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.operations.monitor_day import due_pickup, alert_time

AT = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)


def order(status='NEW', at=None):
    return SimpleNamespace(id='order', status=status, scheduled_at=at or AT,
        created_at=AT - timedelta(days=5), facts={'stops':[
            {'id':'pickup','kind':'PICKUP'}, {'id':'drop','kind':'DROPOFF'}]})


def test_due_pickups_include_scheduled_time_and_exclude_future_and_finished_work():
    row = order()
    assert due_pickup(row, {}, AT)['kind'] == 'DUE'
    assert due_pickup(row, {}, AT)['at'] == AT
    assert due_pickup(order(at=AT+timedelta(seconds=1)), {}, AT) is None
    for status in ('IN_PROGRESS', 'COMPLETED', 'CANCELLED'):
        assert due_pickup(order(status), {}, AT) is None
    for status, arrived in [('COMPLETED',None),('PENDING',AT)]:
        visit=SimpleNamespace(status=status,arrived_at=arrived)
        assert due_pickup(row, {'pickup':visit}, AT) is None


def test_alert_sorting_uses_the_deadline_instead_of_old_order_creation_time():
    row=order()
    row.facts['stops'][0]['window_end']=(AT-timedelta(minutes=3)).isoformat()
    row.facts['stops'][1]['window_end']=(AT-timedelta(minutes=1)).isoformat()
    assert alert_time(row,'LATE_START',AT)==AT-timedelta(minutes=3)
    assert alert_time(row,'AT_RISK',AT)==AT-timedelta(minutes=1)


def test_monitor_lists_due_work_below_a_newer_alert():
    from unittest.mock import Mock, patch
    from uuid import uuid4
    from app.operations import reporting
    row=order(at=AT-timedelta(minutes=2))
    row.number='DUE-1'; row.route_id=None; row.pricing={'status':'PRICED'}
    issue=SimpleNamespace(id='new-alert',version=1,order_id=row.id,kind='OPEN_ISSUE',
        description='New issue',created_at=AT-timedelta(minutes=1),photo_ids=[])
    db=Mock(); db.scalars.side_effect=[[],[row],[issue],[],[],[]]
    with patch.object(reporting,'now',return_value=AT), \
         patch.object(reporting,'company_settings',return_value=(None,SimpleNamespace(time_zone='America/Vancouver'))), \
         patch.object(reporting,'order_attention',return_value={row.id:[]}), \
         patch('app.order_watch.estimates.collect',return_value={}):
        result=reporting.monitor('demo',db,SimpleNamespace(organization_id=uuid4()))
    assert [item['kind'] for item in result['needs_attention']]==['OPEN_ISSUE','DUE']
    assert result['needs_attention'][1]['at']==row.scheduled_at


def test_alert_time_ignores_deadlines_of_already_finished_stops():
    row=order()
    row.facts['stops']=[
        {'id':'finished','kind':'DROPOFF','window_end':(AT-timedelta(hours=3)).isoformat()},
        {'id':'pending','kind':'DROPOFF','window_end':(AT-timedelta(minutes=1)).isoformat()}]
    visits={'finished':SimpleNamespace(status='COMPLETED',arrived_at=AT-timedelta(hours=3))}
    assert alert_time(row,'AT_RISK',AT,visits)==AT-timedelta(minutes=1)
