from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from app.main import app
from app.database import context, engine
from app.models import now
from app.events.models import Event
from app.agents import activity
from app.agents.models import AgentLog, AgentRun
from conftest import HEADERS, login, owner_engine
from test_manual_operations import setup, new_order
from test_email_intake import agent, raw_email

ROOT = '/api/v1/companies/acme/activity'


def seed(accounts):
    own, other, *_ = accounts
    at = now()
    run_id, tied_id, foreign_id = uuid4(), uuid4(), uuid4()
    with Session(owner_engine) as db, db.begin():
        db.add(AgentRun(id=run_id, agent='ORDER', step='Global private step', progress=75))
        db.flush()
        for org, identity, title, stamp in [
            (own['id'], tied_id, 'Validating company booking details.', at),
            (own['id'], uuid4(), 'Reading company email queue.', at - timedelta(seconds=1)),
            (other['id'], foreign_id, 'Other company private step', at),
            (None, uuid4(), 'Global private log', at),
        ]:
            db.add(AgentLog(id=identity, run_id=run_id, organization_id=UUID(org) if org else None, message=title, created_at=stamp))
        for org, identity, kind, dispatcher in [
            (own['id'], tied_id, 'order.created', True),
            (other['id'], uuid4(), 'order.created', True),
            (own['id'], uuid4(), 'message.created', True),
            (own['id'], uuid4(), 'driver.location_updated', True),
            (own['id'], uuid4(), 'watch.question_created', False),
        ]:
            db.add(Event(id=identity, organization_id=UUID(org), type=kind, entity='order', entity_id=uuid4(), actor_type='USER', order_id=uuid4(), correlation_id=uuid4(), created_at=at, dispatchers=dispatcher))
    return tied_id, foreign_id


def test_company_projection_is_private_read_only_and_restores_rls(client, accounts):
    tied, foreign = seed(accounts)
    response = client.get(ROOT)
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page['items']) == 3
    assert {row['source'] for row in page['items']} == {'AGENT', 'EVENT'}
    assert not any(word in str(page) for word in ['Global private', 'Other company', 'message.created', 'driver.location_updated'])
    assert all('run_id' not in row and 'organization_id' not in row and 'progress' not in row for row in page['items'])
    assert all(row['order_id'] is None for row in page['items'])  # Missing/foreign references are never projected.
    assert next(row for row in page['items'] if row['source'] == 'EVENT')['actor'] == 'UNKNOWN'
    assert client.get('/api/v1/companies/other/activity').status_code == 404
    assert client.get('/api/v1/platform/agents/logs').status_code == 403
    assert client.post(ROOT, json={}).status_code == 405
    user = SimpleNamespace(role='DISPATCHER', organization_id=UUID(accounts[0]['id']))
    with Session(engine) as db, db.begin():
        context(db, user.organization_id)
        saved = db.execute(text("SELECT current_setting('app.organization_id'),current_setting('app.platform'),current_setting('app.shipper_id'),current_setting('app.driver_id')")).one()
        activity.page(db, user)
        assert db.execute(text("SELECT current_setting('app.organization_id'),current_setting('app.platform'),current_setting('app.shipper_id'),current_setting('app.driver_id')")).one() == saved
        assert db.scalar(select(func.count()).select_from(AgentLog)) == 0
        with pytest.raises(HTTPException) as denied:
            activity.page(db, user, f'AGENT:{foreign}')
        assert denied.value.status_code == 404
        assert db.scalar(text("SELECT current_setting('app.platform')")) == 'false'


def test_merged_cursor_pages_are_stable_at_ties_and_company_scoped(client, accounts):
    tied, foreign = seed(accounts)
    first = client.get(ROOT, params={'limit': 1}).json()
    assert first['items'][0]['id'] == f'EVENT:{tied}'
    second = client.get(ROOT, params={'limit': 1, 'before': first['next_before']}).json()
    assert second['items'][0]['id'] == f'AGENT:{tied}'
    third = client.get(ROOT, params={'limit': 1, 'before': second['next_before']}).json()
    assert third['items'][0]['title'] == 'Reading company email queue.'
    assert third['next_before'] is None
    assert client.get(ROOT, params={'before': f'AGENT:{foreign}'}).status_code == 404
    assert client.get(ROOT, params={'before': f'EVENT:{uuid4()}'}).status_code == 404
    assert client.get(ROOT, params={'before': 'bad'}).status_code == 422
    assert client.get(ROOT, params={'limit': 101}).status_code == 422
    assert client.get(ROOT, params={'limit': 0}).status_code == 422


def test_activity_requires_current_dispatcher_account(client, accounts):
    seed(accounts)
    with TestClient(app, headers=HEADERS) as anonymous:
        assert anonymous.get(ROOT).status_code == 401
    login(client, 'customer', 'acme', 'customer')
    assert client.get(ROOT).status_code == 403
    login(client)
    assert client.get(ROOT).status_code == 404


def test_existing_execution_events_reach_activity_with_authorized_order(setup):
    client = setup['client']
    created = new_order(setup)
    response = client.get(ROOT)
    assert response.status_code == 200
    entry = next(row for row in response.json()['items'] if row['kind'] == 'order.created')
    assert entry['order_id'] == created['id'] and entry['order_number'] == created['number']
    assert entry['actor'] == 'DISPATCHER' and entry['source'] == 'EVENT'
    assert setup['mobile'].get(ROOT).status_code == 403


def test_new_email_receipt_enters_activity_once_without_private_email_contents(agent):
    from app.intake import service
    client = agent['client']
    company_id = UUID(client.get('/api/v1/auth/me').json()['organization']['id'])
    agent['inbox'].add(raw_email(subject='Private receipt subject', body='Private receipt body'))
    assert service.poll_mailbox(company_id) == 1
    first = client.get(ROOT).json()
    receipts = [entry for entry in first['items'] if entry['kind'] == 'intake.received']
    assert len(receipts) == 1 and receipts[0]['title'] == 'Email received'
    assert receipts[0]['agent'] == 'ORDER'
    assert 'Private receipt' not in str(first) and 'alice@example.com' not in str(first)
    assert service.poll_mailbox(company_id) == 0
    assert [row['id'] for row in client.get(ROOT).json()['items'] if row['kind'] == 'intake.received'] == [receipts[0]['id']]
