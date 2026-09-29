"""Restricted-role PostgreSQL checks for the automation-ready event outbox."""
from uuid import UUID
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import OutboxEvent
from app.operations.outbox import claim, acknowledge, retry
from conftest import login, company


def test_tenant_outbox_claim_retry_and_ack(client):
    login(client)
    acme = company(client)
    other = company(client, 'other')
    own_id, other_id = UUID(acme['id']), UUID(other['id'])
    with Session(engine) as db, db.begin():
        context(db, own_id)
        events = claim(db, own_id, 'worker-1', limit=100)
        assert len(events) >= 1 and events[0].organization_id == own_id
        event_id = events[0].id
        assert events[0].attempts == 1
    with Session(engine) as db, db.begin():
        context(db, other_id)
        assert claim(db, other_id, 'worker-2', limit=1)
        with pytest.raises(HTTPException) as denied:
            acknowledge(db, own_id, event_id, 'worker-2')
        assert denied.value.status_code == 409
    with Session(engine) as db, db.begin():
        context(db, own_id)
        with pytest.raises(ValueError):
            retry(db, own_id, event_id, 'worker-1', 'raw secret: do not store')
        retry(db, own_id, event_id, 'worker-1', 'TEMPORARY_PROVIDER_FAILURE')
        assert claim(db, own_id, 'worker-1') == []
    with Session(engine) as db, db.begin():
        context(db, own_id)
        row = db.get(OutboxEvent, event_id)
        row.available_at = row.created_at
    with Session(engine) as db, db.begin():
        context(db, own_id)
        rows = claim(db, own_id, 'worker-2', limit=1)
        assert rows[0].id == event_id and rows[0].attempts == 2
        acknowledge(db, own_id, event_id, 'worker-2')
    with Session(engine) as db, db.begin():
        context(db, own_id)
        assert claim(db, own_id, 'worker-2') == []
