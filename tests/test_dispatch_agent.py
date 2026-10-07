"""Dispatch agent: feasible candidates, rule fallback, AI ranking limits, approval, AUTO mode and isolation."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.dispatch import rank, service, worker
from conftest import login, company, post, HEADERS, owner_engine
from test_manual_operations import BASE, check, address, new_order, setup  # noqa: F401  (setup is a fixture)


def mode(client, value):
    settings = check(client.get(BASE + '/settings'))
    return check(client.put(BASE + '/settings', json={'version': settings['version'], 'data': settings['data'] | {'dispatch_mode': value}},
        headers={'Idempotency-Key': str(uuid4())}))


def suggest(client, order, refresh=False):
    return client.post(BASE + f'/orders/{order["id"]}/dispatch-suggestions' + ('?refresh=true' if refresh else ''))


def titles(client): return [n['title'] for n in check(client.get(BASE + '/notifications'))]


def age(minutes=10):
    with owner_engine.begin() as c: c.execute(text('UPDATE dispatch_decisions SET created_at = created_at - make_interval(mins => :m)'), {'m': minutes})


@pytest.fixture
def fleet(setup, monkeypatch):
    """Dana (on duty, attached van) plus Omar (off duty) and Nia (no vehicle). AI is off unless a test enables it."""
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    client = setup['client']
    omar_van = check(post(client, BASE + '/vehicles', dict(name='Spare Van', type_id=setup['type_id'], plate='SPARE1', province='BC', payload_kg=1000,
        volume_m3=12, length_cm=300, width_cm=180, height_cm=180)), 201)
    omar = check(post(client, BASE + '/drivers', dict(name='Omar Offduty', email='omar@example.com', phone='6045550103', address=address(), vehicle_id=omar_van['id'])), 201)
    nia = check(post(client, BASE + '/drivers', dict(name='Nia Novan', email='nia@example.com', phone='6045550104', address=address())), 201)
    return setup | dict(omar=omar, nia=nia)


def test_suggestion_ranks_feasible_drivers_and_approval_assigns(fleet):
    w = fleet; client = w['client']; order = new_order(w)
    decision = check(suggest(client, order))
    assert decision['status'] == 'SUGGESTED' and decision['mode'] == 'MANUAL'
    assert decision['ranked_by'] == 'RULES' and decision['error_code'] == 'AI_NOT_CONFIGURED'
    assert [c['driver_id'] for c in decision['candidates']] == [w['driver']['id']]
    best = decision['candidates'][0]
    assert best['vehicle_id'] == w['vehicle']['id'] and best['route_id'] is None and best['reason']
    assert best['metrics']['position'] == 'HOME' and best['metrics']['deadhead_km'] is not None
    reasons = {item['driver_id']: item['reason'] for item in decision['excluded']}
    assert 'On Duty' in reasons[w['omar']['id']] and 'No vehicle' in reasons[w['nia']['id']]
    # A recent suggestion for the same Order version is reused; refresh evaluates again.
    assert check(suggest(client, order))['id'] == decision['id']
    decision = check(suggest(client, order, refresh=True))
    assert check(client.get(BASE + f'/orders/{order["id"]}/dispatch-decisions'))[0]['id'] == decision['id']
    approval = dict(version=decision['version'], driver_id=w['omar']['id'])
    assert post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', approval).status_code == 422
    key = str(uuid4()); approval['driver_id'] = w['driver']['id']
    assigned = check(post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', approval, key))
    assert assigned['order_id'] == order['id'] and assigned['route']['driver_id'] == w['driver']['id']
    assert check(post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', approval, key)) == assigned
    assert post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', {**approval, 'version': 2}).status_code == 409
    stored = check(client.get(BASE + f'/orders/{order["id"]}/dispatch-decisions'))[0]
    assert stored['status'] == 'ASSIGNED' and stored['driver_id'] == w['driver']['id'] and stored['decided_by']
    assert check(client.get(BASE + f'/orders/{order["id"]}'))['status'] == 'ASSIGNED'
    assert suggest(client, order).status_code == 409


def test_second_order_joins_the_planned_route(fleet):
    w = fleet; client = w['client']
    first = new_order(w)
    decision = check(suggest(client, first))
    check(post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', dict(version=1, driver_id=w['driver']['id'])))
    second = new_order(w)
    decision = check(suggest(client, second))
    best = decision['candidates'][0]
    assert best['route_id'] and best['metrics']['route_orders'] == 1 and 'Joins the planned route' in best['facts'][0]
    route = check(post(client, BASE + f'/dispatch-decisions/{decision["id"]}/approve', dict(version=1, driver_id=w['driver']['id'])))['route']
    assert route['id'] == best['route_id'] and len(route['stops']) == 4


def test_ai_orders_only_the_given_candidates(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    answer = rank.Ranking(ranking=[rank.Ranked(key='c9', reason='invented'), rank.Ranked(key='c2', reason='Closest to pickup'),
        rank.Ranked(key='c2', reason='duplicate')], summary='Second driver is closest.')
    fake = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: SimpleNamespace(output_parsed=answer)))
    monkeypatch.setattr(rank.openai, 'OpenAI', lambda **kwargs: fake)
    candidates = [{'driver_id': f'd{n}', 'score': n, 'metrics': {}, 'first_arrival': '', 'vehicle_name': 'Van', 'facts': ['x'], 'reason': ''} for n in (1, 2, 3)]
    ordered, summary = rank.rank({}, candidates)
    assert [c['driver_id'] for c in ordered] == ['d2', 'd1', 'd3'] and ordered[0]['reason'] == 'Closest to pickup'
    assert summary == 'Second driver is closest.'
    fake.responses.parse = lambda **kwargs: SimpleNamespace(output_parsed=rank.Ranking(ranking=[rank.Ranked(key='c7', reason='x')], summary=''))
    with pytest.raises(rank.Unavailable): rank.rank({}, candidates)


def test_ai_ranking_is_recorded(fleet, monkeypatch):
    w = fleet; order = new_order(w)
    monkeypatch.setattr(rank, 'rank', lambda brief, candidates: ([{**candidates[0], 'reason': 'Only driver on duty'}], 'Dana is the only option.'))
    decision = check(suggest(w['client'], order))
    assert decision['ranked_by'] == 'AI' and decision['error_code'] is None and decision['summary'] == 'Dana is the only option.'
    assert decision['candidates'][0]['reason'] == 'Only driver on duty'


def test_manual_mode_worker_never_assigns(fleet):
    w = fleet; order = new_order(w)
    assert worker.cycle() == {}
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}'))['status'] == 'NEW'


def test_auto_mode_assigns_as_agent_and_notifies(fleet):
    w = fleet; client = w['client']
    assert mode(client, 'AUTO')['data']['dispatch_mode'] == 'AUTO'
    order = new_order(w)
    assert worker.cycle()['ASSIGNED'] == 1
    assert check(client.get(BASE + f'/orders/{order["id"]}'))['status'] == 'ASSIGNED'
    decision = check(client.get(BASE + f'/orders/{order["id"]}/dispatch-decisions'))[0]
    assert decision['mode'] == 'AUTO' and decision['status'] == 'ASSIGNED' and decision['decided_by'] is None
    assert decision['driver_id'] == w['driver']['id']
    assert 'Order auto-assigned' in titles(client)
    assert 'Order assigned' in [n['title'] for n in check(w['mobile'].get(BASE + '/driver/notifications'))]
    with owner_engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM audit_events WHERE action = 'order.assigned' AND actor_id IS NULL")).scalar() == 1
        assert c.execute(text("SELECT actor_type FROM events WHERE type = 'order.assigned'")).scalar() == 'AGENT'
    assert worker.cycle() == {}


def test_auto_mode_without_a_driver_flags_once_then_assigns(fleet):
    w = fleet; client = w['client']; mobile = w['mobile']
    duty = w['duty']
    check(post(mobile, BASE + '/driver/duty/' + duty['id'] + '/end', dict(version=duty['version'], ended_at=service.now().isoformat())))
    mode(client, 'AUTO'); order = new_order(w)
    assert worker.cycle()['NO_CANDIDATE'] == 1
    attention = [item for item in check(client.get(BASE + '/monitor'))['needs_attention'] if item['kind'] == 'NO_DRIVER']
    assert attention and attention[0]['order_id'] == order['id'] and 'On Duty' in attention[0]['description']
    assert titles(client).count('No driver available') == 1
    assert worker.cycle() == {}  # evaluated recently
    age(); assert worker.cycle()['NO_CANDIDATE'] == 1
    assert titles(client).count('No driver available') == 1
    check(post(mobile, BASE + '/driver/duty', {}), 201)
    age(); assert worker.cycle()['ASSIGNED'] == 1
    assert not [item for item in check(client.get(BASE + '/monitor'))['needs_attention'] if item['kind'] == 'NO_DRIVER']


def test_auto_mode_skips_orders_outside_the_horizon(fleet):
    w = fleet; mode(w['client'], 'AUTO')
    later = new_order(w, scheduled_at=(service.now() + service.HORIZON + timedelta(hours=2)).isoformat())
    assert worker.cycle() == {}
    assert check(w['client'].get(BASE + f'/orders/{later["id"]}'))['status'] == 'NEW'


def test_dispatch_mode_values_and_access(fleet):
    w = fleet; client = w['client']
    settings = check(client.get(BASE + '/settings'))
    bad = client.put(BASE + '/settings', json={'version': settings['version'], 'data': settings['data'] | {'dispatch_mode': 'SMART'}}, headers={'Idempotency-Key': str(uuid4())})
    assert bad.status_code == 422
    order = new_order(w)
    decision = check(suggest(client, order))
    with TestClient(app, headers=HEADERS) as shipper_client:
        login(shipper_client, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        assert suggest(shipper_client, order).status_code == 403
    with TestClient(app, headers=HEADERS) as other:
        login(other); company(other, 'other'); login(other, 'dispatch', 'other', 'dispatcher')
        assert other.post(f'/api/v1/companies/other/orders/{order["id"]}/dispatch-suggestions').status_code == 404
        assert post(other, f'/api/v1/companies/other/dispatch-decisions/{decision["id"]}/approve', dict(version=1, driver_id=w['driver']['id'])).status_code == 404
