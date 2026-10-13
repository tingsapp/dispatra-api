"""Run separately: encrypted verification outbox, without sending unrelated company emails."""
import argparse
import os
import time
from types import SimpleNamespace
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import EmailVerification, OutboxEvent, now
from app.intake import vault
from app.operations import outbox, smtp_transport

EVENT = 'verification.requested'

def process(org_id, event_id, worker):
    with Session(engine) as db, db.begin():
        context(db, org_id)
        event = outbox._owned(db, org_id, event_id, worker)
        row = db.scalar(select(EmailVerification).where(EmailVerification.id == event.entity_id).with_for_update())
        if not row or row.consumed_at or row.expires_at <= now() or row.delivery_status in {'SENT','FAILED','UNKNOWN'}:
            if row: row.secret = None
            outbox.acknowledge(db, org_id, event_id, worker); return
        if row.delivery_status == 'SENDING':
            row.delivery_status = 'UNKNOWN'; row.secret = None
            outbox.acknowledge(db, org_id, event_id, worker); return
        code = vault.unseal(row.secret, 'verification message')
        row.delivery_status = 'SENDING'
        message = SimpleNamespace(recipient=row.email, subject='Verify your Dispatra account',
            message_id=f'<verification-{row.id}@dispatra.com>',
            body_text=f'Your Dispatra verification code is {code}. It expires in 10 minutes. If you did not create this account, ignore this email.',
            body_html=f'<p>Your Dispatra verification code is <strong>{code}</strong>.</p><p>It expires in 10 minutes. If you did not create this account, ignore this email.</p>')
    status, failure = 'SENT', None
    try: smtp_transport.send(message)
    except smtp_transport.TemporaryFailure as exc: status, failure = 'PENDING', exc.code
    except smtp_transport.PermanentFailure as exc: status, failure = 'FAILED', exc.code
    except smtp_transport.UnknownAcceptance as exc: status, failure = 'UNKNOWN', exc.code
    with Session(engine) as db, db.begin():
        context(db, org_id)
        event = outbox._owned(db, org_id, event_id, worker)
        row = db.get(EmailVerification, event.entity_id)
        if row:
            row.delivery_status = 'FAILED' if status == 'PENDING' and event.attempts >= 5 else status
            if row.delivery_status != 'PENDING': row.secret = None
        if row and row.delivery_status == 'PENDING': outbox.retry(db, org_id, event_id, worker, failure)
        else: outbox.acknowledge(db, org_id, event_id, worker)

def poll_once(worker='verification-worker'):
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        organizations = db.scalars(select(OutboxEvent.organization_id).distinct().where(
            OutboxEvent.event_type == EVENT, OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now(),
            or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < now() - outbox.LEASE))).all()
    count = 0
    for org in organizations:
        with Session(engine) as db, db.begin():
            context(db, org)
            identifiers = [event.id for event in outbox.claim(db, org, worker, 25, event_types=(EVENT,))]
        for identifier in identifiers: process(org, identifier, worker); count += 1
    return count

def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--once', action='store_true'); args = parser.parse_args()
    while True:
        try: poll_once('verification-' + str(os.getpid()))
        except Exception as exc:
            print(f'Verification worker error: {type(exc).__name__}', flush=True)
            if args.once: raise
        if args.once: return
        time.sleep(2)

if __name__ == '__main__': main()
