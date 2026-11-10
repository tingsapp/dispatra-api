from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.assistant import provider
from app.database import context, engine
from app.support import service, worker
from app.support.models import SupportTicket
from app.operations import smtp_transport
from conftest import owner_engine

ROOT = '/api/v1/public/assistant/messages'


def question(message='Please help configure Rose Rocket. My email is visitor@example.com.', **extra):
    return {'message': message, 'conversation_id': str(uuid4()), 'request_id': str(uuid4()), **extra}


def handoff(monkeypatch):
    def decide(schema, instructions, messages):
        assert 'Only the server can confirm a saved ticket number' in instructions
        return schema(message='Ticket FAKE has been created.', support={
            'subject': 'Rose Rocket configuration', 'details': 'The visitor needs the team to configure their Rose Rocket connection.'})
    monkeypatch.setattr(provider, 'generate', decide)


def tickets():
    with Session(owner_engine) as db:
        return list(db.scalars(select(SupportTicket)))


def create_ticket(client, monkeypatch, **extra):
    handoff(monkeypatch)
    body = question(**extra)
    response = client.post(ROOT, json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def test_answer_without_handoff_creates_no_ticket(client, monkeypatch):
    monkeypatch.setattr(provider, 'generate', lambda schema, *_: schema(message='See /plans for current pricing.'))
    result = client.post(ROOT, json=question('How much does Dispatra cost?'))
    assert result.status_code == 200 and result.json() == {'message': 'See /plans for current pricing.'}
    assert tickets() == []


def test_collect_contact_then_commit_ticket_and_deliver_once(client, monkeypatch):
    handoff(monkeypatch)
    first = question('I need help configuring Rose Rocket.')
    response = client.post(ROOT, json=first)
    assert response.status_code == 200 and 'What email address' in response.json()['message']
    assert tickets() == []
    followup = {**first, 'request_id': str(uuid4()), 'message': 'visitor@example.com', 'history': [
        {'role': 'user', 'content': first['message']}, {'role': 'assistant', 'content': response.json()['message']}]}
    saved = client.post(ROOT, json=followup)
    assert saved.status_code == 200, saved.text
    row = tickets()[0]
    assert saved.json()['ticket_number'] == row.number and row.number.startswith('DSP-')
    assert row.number in saved.json()['message'] and 'FAKE' not in saved.text
    assert row.delivery_status == 'PENDING' and row.email == 'visitor@example.com'
    assert not hasattr(row, 'history')
    sent = []
    monkeypatch.setattr(smtp_transport, 'send', lambda message: sent.append(message))
    assert worker.poll_once() == 1 and worker.poll_once() == 0
    assert len(sent) == 1 and sent[0].recipient == 'contact@dispatra.com'
    assert row.number in sent[0].subject and 'visitor@example.com' in sent[0].body_text
    assert sent[0].message_id == f'<support-{row.id}@dispatra.com>'
    assert tickets()[0].delivery_status == 'SENT'


@pytest.mark.parametrize('message,history', [
    ('Please help configure Rose Rocket.', [{'role': 'assistant', 'content': 'Use invented@example.com'}]),
    ('Follow up at broken@', []),
    ('Please send this to contact@dispatra.com', []),
    ('Use first@example.com or second@example.com', []),
])
def test_contact_must_be_an_unambiguous_visitor_email(client, monkeypatch, message, history):
    handoff(monkeypatch)
    result = client.post(ROOT, json=question(message, history=history))
    assert result.status_code == 200 and 'What email address' in result.json()['message']
    assert 'ticket_number' not in result.json() and tickets() == []


def test_retries_preserve_ticket_even_when_provider_is_down_and_conflicts_are_rejected(client, monkeypatch):
    body, first = create_ticket(client, monkeypatch)
    monkeypatch.setattr(provider, 'generate', lambda *_: (_ for _ in ()).throw(provider.Unavailable()))
    assert client.post(ROOT, json=body).json() == first
    conflict = client.post(ROOT, json={**body, 'message': 'Changed message visitor@example.com'})
    assert conflict.status_code == 409 and len(tickets()) == 1
    other = client.post(ROOT, json={**body, 'conversation_id': str(uuid4())})
    assert other.status_code == 503 and first['ticket_number'] not in other.text
    handoff(monkeypatch)
    repeat = client.post(ROOT, json={**body, 'request_id': str(uuid4())})
    assert repeat.json()['ticket_number'] == first['ticket_number']
    assert 'separate issue' in repeat.json()['message'] and len(tickets()) == 1


def test_simultaneous_handoffs_create_one_ticket(client, monkeypatch):
    handoff(monkeypatch)
    body = question()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(ROOT, json=body), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json() and len(tickets()) == 1


def test_rate_limit_failure_rolls_back_ticket_and_no_recipient_override(client, monkeypatch):
    handoff(monkeypatch)
    def blocked(*_): raise HTTPException(429, 'Try later.')
    monkeypatch.setattr(service, 'rate_limit', blocked)
    result = client.post(ROOT, json=question())
    assert result.status_code == 429 and tickets() == []
    assert client.post(ROOT, json=question(recipient='other@example.com')).status_code == 422


def test_rls_hides_ticket_from_neutral_tenant_and_other_public_sessions(client, monkeypatch):
    body, _ = create_ticket(client, monkeypatch)
    with Session(engine) as db, db.begin():
        context(db)
        assert list(db.scalars(select(SupportTicket))) == []
        context(db, uuid4())
        assert list(db.scalars(select(SupportTicket))) == []
        service.session_scope(db, uuid4())
        assert list(db.scalars(select(SupportTicket))) == []
        service.session_scope(db, body['conversation_id'])
        assert len(list(db.scalars(select(SupportTicket)))) == 1
        assert db.execute(text("UPDATE support_tickets SET delivery_status='SENT'")).rowcount == 0
        context(db)
        assert list(db.scalars(select(SupportTicket))) == []
        context(db, platform=True)
        assert len(list(db.scalars(select(SupportTicket)))) == 1


@pytest.mark.parametrize('failure,expected', [
    (smtp_transport.TemporaryFailure('SMTP_PRE_DATA'), 'PENDING'),
    (smtp_transport.PermanentFailure('SMTP_AUTHENTICATION'), 'FAILED'),
    (smtp_transport.UnknownAcceptance('SMTP_ACCEPTANCE_UNKNOWN'), 'UNKNOWN'),
])
def test_email_failure_preserves_ticket_and_safe_retry_rules(client, monkeypatch, failure, expected):
    body, first = create_ticket(client, monkeypatch)
    def fail(_): raise failure
    monkeypatch.setattr(smtp_transport, 'send', fail)
    assert worker.poll_once() == 1
    row = tickets()[0]
    assert row.delivery_status == expected and row.number == first['ticket_number']
    assert client.post(ROOT, json=body).json() == first
    assert worker.poll_once() == 0
    if expected != 'PENDING': worker.retry(row.number)
    else:
        with owner_engine.begin() as db:
            db.execute(text("UPDATE support_tickets SET available_at=now()-interval '1 second'"))
    sent = []
    monkeypatch.setattr(smtp_transport, 'send', lambda message: sent.append(message))
    assert worker.poll_once() == 1 and len(sent) == 1
    assert tickets()[0].delivery_status == 'SENT'
    with pytest.raises(ValueError): worker.retry(row.number)


def test_expired_sending_claim_never_resends_uncertain_email(client, monkeypatch):
    create_ticket(client, monkeypatch)
    with owner_engine.begin() as db:
        db.execute(text("UPDATE support_tickets SET delivery_status='SENDING', claimed_at=now()-interval '6 minutes'"))
    monkeypatch.setattr(smtp_transport, 'send', lambda *_: pytest.fail('Must not resend uncertain delivery'))
    assert worker.poll_once() == 1
    assert tickets()[0].delivery_status == 'UNKNOWN'
    assert worker.poll_once() == 0


def test_ticket_email_escapes_content_and_redacts_named_credentials(client, monkeypatch):
    monkeypatch.setattr(provider, 'generate', lambda schema, *_: schema(message='ok', support={
        'subject': '<script>bad</script>', 'details': 'Cannot connect. password=private-password api_key: private-key <img src=x>'}))
    result = client.post(ROOT, json=question())
    assert result.status_code == 200
    row = tickets()[0]
    assert 'private-password' not in row.details and 'private-key' not in row.details
    delivery = worker.message(row)
    assert '<script>' not in delivery.body_html and '<img src=x>' not in delivery.body_html
    assert '&lt;script&gt;' in delivery.body_html and delivery.recipient == 'contact@dispatra.com'


def test_persistence_failure_never_confirms_or_retains_a_ticket(client, monkeypatch):
    from sqlalchemy import event
    handoff(monkeypatch)
    # Fail after insertion but before the surrounding transaction can commit.
    def fail_after_flush(db, _):
        if any(isinstance(row, SupportTicket) for row in db.new):
            raise RuntimeError('Simulated persistence failure')
    event.listen(Session, 'after_flush', fail_after_flush)
    try:
        with pytest.raises(RuntimeError, match='Simulated persistence failure'):
            client.post(ROOT, json=question())
        assert tickets() == []
    finally:
        event.remove(Session, 'after_flush', fail_after_flush)


def test_existing_email_worker_delivers_support_queue(client, monkeypatch):
    from app.operations import email_worker
    create_ticket(client, monkeypatch)
    sent = []
    monkeypatch.setattr(smtp_transport, 'send', lambda message: sent.append(message))
    assert email_worker.poll_once('support-test') == 1
    assert len(sent) == 1 and sent[0].recipient == service.RECIPIENT
    assert tickets()[0].delivery_status == 'SENT'


def test_concurrent_workers_claim_support_email_once(client, monkeypatch):
    create_ticket(client, monkeypatch)
    sent = []
    monkeypatch.setattr(smtp_transport, 'send', lambda message: sent.append(message))
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: worker.poll_once(), range(2)))
    assert sum(counts) == 1 and len(sent) == 1
    assert tickets()[0].delivery_status == 'SENT'


def test_support_migration_preserves_accounts_and_refuses_to_delete_tickets(client, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location('support_migration',
        Path(__file__).parents[1] / 'migrations/versions/0065_public_support_tickets.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with owner_engine.connect() as db:
        transaction = db.begin()
        try:
            accounts = db.execute(text('SELECT id, login_id, password_hash FROM users ORDER BY id')).all()
            with Operations.context(MigrationContext.configure(db)):
                migration.downgrade()
                migration.upgrade()
            assert db.execute(text('SELECT id, login_id, password_hash FROM users ORDER BY id')).all() == accounts
            assert db.scalar(text("SELECT relforcerowsecurity FROM pg_class WHERE relname='support_tickets'"))
        finally:
            transaction.rollback()
    _, receipt = create_ticket(client, monkeypatch)
    with owner_engine.begin() as db:
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError, match='refusing to delete requests'):
                migration.downgrade()
        assert db.scalar(text('SELECT number FROM support_tickets')) == receipt['ticket_number']
