"""Operational Analytics: measured multi-delivery outcomes, accepted POD, scope and exports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import csv
import io
from sqlalchemy import text
from conftest import login, company, post, owner_engine
from test_manual_operations import BASE, check, new_order, booking, assign, finish, setup  # noqa: F401


def test_summary_distinguishes_driver_proof_from_dispatcher_completion(setup):
    w = setup; client = w['client']
    delivered = new_order(w)
    finish(w, assign(w, delivered))
    payload = booking(w['shipper']['id'], w['service'], w['type_id'])
    payload['stops'][1]['window_end'] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    manual = check(post(client, BASE + '/orders', payload), 201)
    assign(w, manual)
    manual = check(client.get(BASE + f'/orders/{manual["id"]}'))
    check(post(client, BASE + f'/orders/{manual["id"]}/complete', {'version': manual['version']}))
    pending = new_order(w)
    assign(w, pending)
    check(post(w['mobile'], BASE + f'/driver/stops/{pending["facts"]["stops"][0]["id"]}/issue',
        {'kind': 'OTHER', 'description': 'Loading bay closed'}), 201)
    report = check(client.get(BASE + '/analytics'))
    assert report['orders'] == 3 and report['completed_orders'] == 2
    assert report['pod_verified_orders'] == 1 and report['open_issues'] == 1
    assert report['source_counts'] == {'DISPATCHER': 3}
    assert report['sla_known_orders'] == 0 and report['on_time_percent'] is None
    rows = {row['id']: row for row in report['rows']}
    assert rows[delivered['id']]['shipper_id'] == w['shipper']['id']
    assert rows[delivered['id']]['driver_id'] == w['driver']['id']
    assert rows[delivered['id']]['pod_verified'] is True
    assert rows[manual['id']]['pod_verified'] is False
    assert rows[pending['id']]['open_issues'] == 1
    assert sum(day['not_measured'] for day in report['daily']) == 2
    assert not {'completed_revenue_before_tax', 'accessorial_counts', 'hourly_completed'} & report.keys()
    assert 'total' not in rows[delivered['id']]


def test_multi_delivery_outcome_and_pod_require_every_dropoff(setup):
    w = setup; client = w['client']
    payload = booking(w['shipper']['id'], w['service'], w['type_id'])
    second = deepcopy(payload['stops'][1]); second['id'] = str(uuid4()); payload['stops'].append(second)
    item = deepcopy(payload['items'][0]); item.update(id=str(uuid4()), delivery_id=second['id']); payload['items'].append(item)
    end = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    for stop in payload['stops'][1:]: stop['window_end'] = end
    order = check(post(client, BASE + '/orders', payload), 201)
    finish(w, assign(w, order))
    report = check(client.get(BASE + '/analytics'))
    assert report['on_time_percent'] == 100 and report['pod_verified_orders'] == 1
    facts = deepcopy(order['facts'])
    facts['stops'][1]['window_end'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with owner_engine.begin() as db:
        db.execute(text('update orders set facts = cast(:facts as jsonb), source = :source where id = :id'),
            {'facts': __import__('json').dumps(facts), 'source': 'EMAIL', 'id': order['id']})
    report = check(client.get(BASE + '/analytics'))
    assert report['rows'][0]['delivery_outcome'] == 'LATE'
    assert report['on_time_percent'] == 0 and report['daily'][0]['late'] == 1
    assert report['source_counts'] == {'EMAIL': 1}
    # An accepted evidence ID must still resolve to this delivery's evidence.
    with owner_engine.begin() as db:
        db.execute(text('delete from delivery_evidence where stop_id = :id'), {'id': second['id']})
    assert check(client.get(BASE + '/analytics'))['pod_verified_orders'] == 0
    facts['stops'][1]['window_end'] = None
    with owner_engine.begin() as db:
        db.execute(text('update orders set facts = cast(:facts as jsonb) where id = :id'), {'facts': __import__('json').dumps(facts), 'id': order['id']})
    report = check(client.get(BASE + '/analytics'))
    assert report['sla_known_orders'] == 0 and report['rows'][0]['delivery_outcome'] == 'NOT_MEASURED'


def test_date_range_export_and_tenant_access(setup):
    w = setup; client = w['client']
    now = datetime.now(timezone.utc)
    inside = new_order(w, scheduled_at=now.isoformat())
    new_order(w, scheduled_at=(now + timedelta(days=2)).isoformat())
    params = {'date_from': (now - timedelta(hours=1)).isoformat(), 'date_to': (now + timedelta(hours=1)).isoformat()}
    report = check(client.get(BASE + '/analytics', params=params))
    assert report['orders'] == 1 and report['rows'][0]['id'] == inside['id']
    assert report['rows'][0]['shipper_id'] == w['shipper']['id']
    assert report['rows'][0]['driver_id'] is None
    csv_response = client.get(BASE + '/analytics/export.csv', params=params)
    assert csv_response.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_response.text)))
    assert 'Total' not in rows[0] and len(rows) == 2 and rows[1][0] == inside['number']
    assert client.get(BASE + '/analytics', params={'date_from': '2026-10-01T00:00:00'}).status_code == 422
    assert w['mobile'].get(BASE + '/analytics').status_code == 403
    login(client); company(client, 'other'); login(client, 'dispatch', 'other', 'dispatcher')
    assert client.get(BASE + '/analytics').status_code == 404
    assert check(client.get('/api/v1/companies/other/analytics'))['orders'] == 0
