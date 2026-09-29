"""Separate transactional-outbox worker for explicit quote/invoice email requests.

Run `python -m app.operations.email_worker` alongside the API. One claim is
committed before network I/O. A crash after entering SENDING becomes UNKNOWN
and requires a dispatcher decision, so an accepted invoice is not auto-sent twice.
"""
import argparse
import os
import time
from uuid import UUID
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import OutboxEvent, User, now
from app.services import audit
from . import outbox, smtp_transport
from .models import EmailDelivery

EVENT_TYPE = 'email.requested'


def pending_companies():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        observed_at = now()
        return db.scalars(select(OutboxEvent.organization_id).distinct().where(
            OutboxEvent.event_type == EVENT_TYPE,
            OutboxEvent.published_at.is_(None),
            OutboxEvent.available_at <= observed_at,
            or_(OutboxEvent.claimed_at.is_(None),
                OutboxEvent.claimed_at < observed_at - outbox.LEASE))).all()


def _transition(db, event, status, error_code=None):
    row = db.scalar(select(EmailDelivery).where(
        EmailDelivery.organization_id == event.organization_id,
        EmailDelivery.id == event.entity_id).with_for_update())
    if not row:
        outbox.acknowledge(db, event.organization_id, event.id, event.claimed_by)
        return None
    row.status = status
    row.error_code = error_code
    row.version += 1
    if status == 'SENT': row.sent_at = now()
    if status in {'SENT', 'FAILED', 'UNKNOWN'}:
        actor = db.get(User, row.requested_by)
        audit(db, actor, 'email.' + status.lower(), row.id, row.organization_id)
    return row


def process_event(organization_id: UUID, event_id: UUID, worker_id: str):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        event = outbox._owned(db, organization_id, event_id, worker_id)
        row = db.scalar(select(EmailDelivery).where(
            EmailDelivery.organization_id == organization_id,
            EmailDelivery.id == event.entity_id).with_for_update())
        if row is None or row.status in {'SENT', 'FAILED', 'UNKNOWN'}:
            outbox.acknowledge(db, organization_id, event_id, worker_id)
            return
        if row.status == 'SENDING':
            _transition(db, event, 'UNKNOWN', 'WORKER_INTERRUPTED')
            outbox.acknowledge(db, organization_id, event_id, worker_id)
            return
        _transition(db, event, 'SENDING')
        # Snapshot the exact message before releasing the transaction.
        message = row
        db.expunge(message)

    try:
        smtp_transport.send(message)
    except smtp_transport.TemporaryFailure as failure:
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            event = outbox._owned(db, organization_id, event_id, worker_id)
            if event.attempts >= 5:
                _transition(db, event, 'FAILED', failure.code)
                outbox.acknowledge(db, organization_id, event_id, worker_id)
            else:
                _transition(db, event, 'PENDING', failure.code)
                outbox.retry(db, organization_id, event_id, worker_id, failure.code)
    except (smtp_transport.PermanentFailure, smtp_transport.UnknownAcceptance) as failure:
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            event = outbox._owned(db, organization_id, event_id, worker_id)
            status = 'UNKNOWN' if isinstance(failure, smtp_transport.UnknownAcceptance) else 'FAILED'
            _transition(db, event, status, failure.code)
            outbox.acknowledge(db, organization_id, event_id, worker_id)
    else:
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            event = outbox._owned(db, organization_id, event_id, worker_id)
            _transition(db, event, 'SENT')
            outbox.acknowledge(db, organization_id, event_id, worker_id)


def poll_once(worker_id: str, limit: int = 25):
    processed = 0
    for organization_id in pending_companies():
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            events = outbox.claim(db, organization_id, worker_id, limit,
                event_types=(EVENT_TYPE,))
            identifiers = [event.id for event in events]
        for event_id in identifiers:
            process_event(organization_id, event_id, worker_id)
            processed += 1
    return processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args()
    worker_id = 'smtp-' + str(os.getpid())
    while True:
        try:
            count = poll_once(worker_id)
            if args.once:
                print(f'Processed {count} email request(s).')
                return
        except Exception as exc:
            # Never include SMTP responses, credentials or message content in logs.
            print(f'Email worker error: {type(exc).__name__}', flush=True)
            if args.once: raise
        time.sleep(max(1, args.interval))


if __name__ == '__main__': main()
