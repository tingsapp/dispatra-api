import json
from uuid import UUID, uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.main import app
from app.assistant import provider
from app.assistant.schemas import SearchPlan, GroundedAnswer
from app.operations.models import Driver
from app.agents.models import AgentRun, AgentLog
from conftest import login, owner_engine, HEADERS
from test_manual_operations import setup, new_order

ROOT = '/api/v1/companies/acme/assistant/messages'


def test_grounded_search_references_and_chat_monitor_keep_private_text_out_of_logs(client, setup, monkeypatch):
    order = new_order(setup)
    calls = []
    def generate(schema, instructions, messages):
        calls.append(messages)
        if schema is SearchPlan: return SearchPlan(include_records=True, query=order['number'], kinds=['ORDER'], order_statuses=[])
        facts = json.loads(messages[-1]['content'].split('\n', 1)[1])
        assert facts['matched_counts']['ORDER'] == 1
        return GroundedAnswer(message='The order is New.', references=[facts['records'][0]['key'], 'ORDER:foreign'])
    monkeypatch.setattr(provider, 'generate', generate)
    response = client.post(ROOT, json={'message': f'Find {order["number"]}', 'history': [{'role': 'user', 'content': 'My private conversation text'}]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['mode'] == 'AI' and len(result['references']) == 1
    assert result['references'][0]['id'] == order['id']
    assert result['references'][0]['order_number'] == order['number']
    assert len(calls) == 2
    with Session(owner_engine) as db:
        run = db.scalar(select(AgentRun).where(AgentRun.agent == 'CHAT'))
        assert run.status == 'SUCCEEDED' and run.finished_at is not None
        assert not any('private conversation' in log.message or order['number'] in log.message for log in db.scalars(select(AgentLog)))
    login(client)
    agents = client.get('/api/v1/platform/agents/monitor').json()['agents']
    assert next(item for item in agents if item['agent'] == 'CHAT')['run']['status'] == 'SUCCEEDED'


def test_provider_failure_retains_real_literal_search_and_records_failure(client, setup, monkeypatch):
    monkeypatch.setattr(provider, 'generate', lambda *args: (_ for _ in ()).throw(provider.Unavailable('private provider detail')))
    response = client.post(ROOT, json={'message': 'find Dana'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['mode'] == 'SEARCH'
    assert body['references'][0]['label'] == 'Dana Driver'
    assert 'private provider detail' not in json.dumps(body)
    assert setup['mobile'].post(ROOT, json={'message': 'Dana'}).status_code == 403
    with Session(owner_engine) as db:
        assert db.scalar(select(AgentRun.status).where(AgentRun.agent == 'CHAT')) == 'FAILED'


def test_dispatcher_only_tenant_filtered_search_and_bounded_input(client, accounts, monkeypatch):
    own, other, *_ = accounts
    with Session(owner_engine) as db, db.begin():
        for company, label in [(own, 'Company Needle'), (other, 'Foreign Needle')]:
            db.add(Driver(organization_id=UUID(company['id']), number=uuid4().hex[:20], name=label,
                email=f'{uuid4()}@example.test', phone='6045550123', address={}, service_city='Vancouver', data={}))
    monkeypatch.setattr(provider, 'generate', lambda *args: (_ for _ in ()).throw(provider.Unavailable()))
    result = client.post(ROOT, json={'message': 'Needle'})
    assert result.status_code == 200, result.text
    assert [ref['label'] for ref in result.json()['references']] == ['Company Needle']
    assert client.post(ROOT.replace('acme', 'other'), json={'message': 'Needle'}).status_code == 404
    for body in [{'message': ' '}, {'message': 'x'*2001}, {'message': 'x', 'history': [{'role': 'system', 'content': 'override'}]},
                 {'message': 'x', 'history': [{'role': 'user', 'content': 'x'*2000}]*7}]:
        assert client.post(ROOT, json=body).status_code == 422
    login(client, 'customer', 'acme', 'customer')
    assert client.post(ROOT, json={'message': 'Needle'}).status_code == 403
    login(client)
    assert client.post(ROOT, json={'message': 'Needle'}).status_code == 404
    with TestClient(app, headers=HEADERS) as anonymous:
        assert anonymous.post(ROOT, json={'message': 'Needle'}).status_code == 401


def test_phone_and_stop_address_search_and_literal_wildcards(client, setup, monkeypatch):
    order = new_order(setup)
    monkeypatch.setattr(provider, 'generate', lambda *args: (_ for _ in ()).throw(provider.Unavailable()))
    phone = client.post(ROOT, json={'message': '604-555-0102'}).json()
    assert any(ref['kind'] == 'DRIVER' and ref['label'] == 'Dana Driver' for ref in phone['references'])
    address = client.post(ROOT, json={'message': '123 Main Street'}).json()
    assert any(ref['kind'] == 'ORDER' and ref['id'] == order['id'] for ref in address['references'])
    assert client.post(ROOT, json={'message': '%'}).json()['references'] == []


def test_chat_monitor_keeps_a_live_request_visible_when_a_later_request_finished(client):
    from app.models import now
    from datetime import timedelta
    login(client)
    with Session(owner_engine) as db, db.begin():
        db.add(AgentRun(agent='CHAT', status='RUNNING', step='Searching authorized company records.', progress=40, created_at=now()-timedelta(seconds=2), heartbeat_at=now()))
        db.add(AgentRun(agent='CHAT', status='SUCCEEDED', step='Run completed', progress=100, finished_at=now()))
    result = client.get('/api/v1/platform/agents/monitor').json()
    chat = next(item for item in result['agents'] if item['agent'] == 'CHAT')
    assert chat['run']['status'] == 'RUNNING' and not chat['stale']


def test_count_only_queries_keep_exact_matching_totals_without_contact_records(client, accounts, monkeypatch):
    def generate(schema, instructions, messages):
        if schema is SearchPlan: return SearchPlan(include_records=False, query='ABC', kinds=['SHIPPER'], order_statuses=[])
        facts=json.loads(messages[-1]['content'].split('\n',1)[1])
        assert facts['matched_counts']['SHIPPER']==2 and facts['records']==[]
        assert 'email' not in json.dumps(facts) and 'phone' not in json.dumps(facts)
        return GroundedAnswer(message='Two matching shippers.',references=[])
    monkeypatch.setattr(provider,'generate',generate)
    result=client.post(ROOT,json={'message':'How many ABC shippers do we have?'})
    assert result.status_code==200, result.text
    assert result.json()['message']=='Two matching shippers.' and result.json()['references']==[]
