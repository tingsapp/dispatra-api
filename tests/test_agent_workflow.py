"""Email worker -> AUTO dispatch worker -> driver POD, on restricted-role PostgreSQL.

External IMAP, extraction, geocoding and AI responses are deterministic fixtures;
booking, eligibility, ranking validation, assignment and completion use real services.
"""
import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.dispatch import rank, worker as dispatch_worker
from app.intake import worker as intake_worker
from conftest import HEADERS, login, owner_engine, post
from test_email_intake import agent, facts, queue, raw_email  # noqa: F401 (fixture)
from test_manual_operations import BASE, address, check, finish
from test_dispatch_agent import mode


@pytest.mark.parametrize('ranking', ['RULES', 'AI'])
def test_email_auto_dispatch_driver_completion(agent, monkeypatch, ranking):
    client = agent['client']
    if ranking == 'RULES':
        monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    else:
        monkeypatch.setenv('OPENAI_API_KEY', 'test')

        def respond(**kwargs):
            supplied = json.loads(kwargs['input'][0]['content'])['candidates']
            assert len(supplied) == 2
            assert not any('driver_id' in c or 'address' in c for c in supplied)
            ordered = sorted(supplied, key=lambda c: c['deadhead_km'])
            answer = rank.Ranking(ranking=[rank.Ranked(key=c['key'], reason='Shortest trip to pickup') for c in ordered],
                summary='The closest eligible driver can deliver before the deadline.')
            return SimpleNamespace(output_parsed=answer)

        fake = SimpleNamespace(responses=SimpleNamespace(parse=respond))
        monkeypatch.setattr(rank.openai, 'OpenAI', lambda **kwargs: fake)

    catalog = check(client.get(BASE + '/catalog'))
    type_id = next(c['id'] for c in catalog if c['code'] == 'veh_1_ton')
    drivers = []
    with ExitStack() as sessions:
        for index, (latitude, on_duty) in enumerate([(49.26, True), (49.36, True), (49.26, False)]):
            vehicle = check(post(client, BASE + '/vehicles', dict(name=f'Workflow Van {index}', type_id=type_id,
                plate=f'FLOW{index}', province='BC', payload_kg=1000, volume_m3=12,
                length_cm=300, width_cm=180, height_cm=180)), 201)
            driver = check(post(client, BASE + '/drivers', dict(name=f'Workflow Driver {index}',
                email=f'workflow{index}@example.com', phone='6045550102', address=address(lat=latitude), vehicle_id=vehicle['id'])), 201)
            mobile = sessions.enter_context(TestClient(app, headers=HEADERS))
            login(mobile, 'driver', 'acme', driver['email'], driver['initial_password'])
            if on_duty:
                check(post(mobile, BASE + '/driver/duty', {}), 201)
            drivers.append((driver, mobile))

        mode(client, 'AUTO')
        pickup = datetime.now(timezone.utc) + timedelta(minutes=15)
        extracted = facts(scheduled_at=pickup.isoformat())
        extracted.stops[1].window_end = (pickup + timedelta(hours=3)).isoformat()
        agent['extracted']['value'] = extracted
        raw = raw_email(message_id='<complete-workflow@example.com>')
        agent['inbox'].add(raw)
        agent['inbox'].add(raw)

        assert intake_worker.cycle() == (1, 1)
        [intake] = queue(client)
        assert intake['status'] == 'ORDER_CREATED'
        order = check(client.get(BASE + f'/orders/{intake["order_id"]}'))
        assert order['source'] == 'EMAIL' and order['status'] == 'NEW'
        assert order['facts']['external_reference'] == 'PO-77'
        assert order['pricing']['status'] == 'PRICED'

        assert dispatch_worker.cycle() == {'ASSIGNED': 1}
        assigned = check(client.get(BASE + f'/orders/{order["id"]}'))
        assert assigned['status'] == 'ASSIGNED'
        [decision] = check(client.get(BASE + f'/orders/{order["id"]}/dispatch-decisions'))
        assert decision['ranked_by'] == ranking and decision['mode'] == 'AUTO'
        assert decision['driver_id'] == drivers[0][0]['id']
        assert len(decision['candidates']) == 2
        assert decision['candidates'][0]['metrics']['deadhead_km'] < decision['candidates'][1]['metrics']['deadhead_km']
        [excluded] = decision['excluded']
        assert excluded['driver_id'] == drivers[2][0]['id'] and 'On Duty' in excluded['reason']

        near_driver = drivers[0][1]
        route = check(near_driver.get(BASE + f'/driver/routes/{assigned["route_id"]}'))
        assert finish({'mobile': near_driver}, route)['status'] == 'COMPLETED'
        completed = check(client.get(BASE + f'/orders/{order["id"]}'))
        assert completed['status'] == 'COMPLETED' and completed['completed_at']
        assert completed['pricing'] == order['pricing']
        assert intake_worker.cycle() == (0, 0)
        assert dispatch_worker.cycle() == {}
        with owner_engine.connect() as db:
            assert db.scalar(text('select count(*) from orders')) == 1
            assert db.scalar(text('select count(*) from delivery_evidence')) == 1
            assert db.scalar(text("select count(*) from events where type in ('order.created', 'order.assigned') and actor_type = 'AGENT'")) == 2
        with TestClient(app, headers=HEADERS) as portal:
            login(portal, 'customer', 'acme', agent['shipper']['email'], agent['shipper']['initial_password'])
            visible = check(portal.get(BASE + f'/orders/{order["id"]}'))
            assert visible['status'] == 'COMPLETED' and visible['source'] == 'EMAIL'
            assert check(portal.get(BASE + f'/orders/{order["id"]}/tracking'))['stage'] == 'DELIVERED'
            [proof] = check(portal.get(BASE + f'/orders/{order["id"]}/delivery-proof'))
            assert proof['evidence'][0]['kind'] == 'SIGNATURE'
