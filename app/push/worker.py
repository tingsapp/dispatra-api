"""Delivers queued driver pushes through FCM. Run `python -m app.push.worker` alongside the API.

The claim is committed before network I/O; results are recorded in a second transaction. Delivery is at
least once per phone: a push is retried only when no phone received it, and unregistered tokens are removed.
`--once` drains eligible pushes and exits, so a cron job can run it on hosts without long-lived processes.
"""
import argparse
import os
import time
from uuid import UUID
import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.models import OutboxEvent, now
from app.operations import outbox
from . import fcm
from .models import PushDevice, PushMessage
from .service import EVENT_TYPE, live_devices

MAX_ATTEMPTS = 8


def pending_companies():
    with Session(engine) as db, db.begin():
        context(db, platform=True)
        observed_at = now()
        return db.scalars(select(OutboxEvent.organization_id).distinct().where(OutboxEvent.event_type == EVENT_TYPE,
            OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= observed_at,
            or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < observed_at - outbox.LEASE))).all()


def snapshot(organization_id: UUID, event_id: UUID, worker_id: str):
    """(message fields, tokens), or None when there is nothing left to send."""
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        event = outbox._owned(db, organization_id, event_id, worker_id)
        message = db.get(PushMessage, event.entity_id)
        tokens = [device.token for device in db.scalars(live_devices(organization_id, message.user_id))] if message and not message.sent_at else []
        if not tokens:
            outbox.acknowledge(db, organization_id, event_id, worker_id)
            return None
        return (message.title, message.body, dict(message.data)), tokens


def record(organization_id: UUID, event_id: UUID, worker_id: str, results: dict):
    with Session(engine) as db, db.begin():
        context(db, organization_id)
        event = outbox._owned(db, organization_id, event_id, worker_id)
        gone = [token for token, result in results.items() if result == 'UNREGISTERED']
        if gone: db.execute(delete(PushDevice).where(PushDevice.organization_id == organization_id, PushDevice.token.in_(gone)))
        sent = 'SENT' in results.values()
        if sent: db.get(PushMessage, event.entity_id).sent_at = now()
        if not sent and 'RETRY' in results.values() and event.attempts < MAX_ATTEMPTS:
            outbox.retry(db, organization_id, event_id, worker_id, 'FCM_UNAVAILABLE')
        else:
            outbox.acknowledge(db, organization_id, event_id, worker_id)


def process_event(credentials, client, organization_id: UUID, event_id: UUID, worker_id: str):
    pending = snapshot(organization_id, event_id, worker_id)
    if pending is None: return
    (title, body, data), tokens = pending
    record(organization_id, event_id, worker_id, {token: fcm.send(credentials, client, token, title, body, data) for token in tokens})


def poll_once(credentials, client, worker_id: str, limit: int = 50):
    processed = 0
    for organization_id in pending_companies():
        with Session(engine) as db, db.begin():
            context(db, organization_id)
            identifiers = [event.id for event in outbox.claim(db, organization_id, worker_id, limit, event_types=(EVENT_TYPE,))]
        for event_id in identifiers:
            process_event(credentials, client, organization_id, event_id, worker_id)
            processed += 1
    return processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=3)
    args = parser.parse_args()
    credentials = fcm.Credentials.from_env()
    if credentials is None: raise SystemExit('Set FIREBASE_SERVICE_ACCOUNT_FILE to the Firebase service-account JSON path.')
    worker_id = 'push-' + str(os.getpid())
    with httpx.Client() as client:
        while True:
            try:
                count = poll_once(credentials, client, worker_id)
                if args.once:
                    print(f'Processed {count} push request(s).')
                    return
            except Exception as exc:
                # Never log tokens, credentials or message text.
                print(f'Push worker error: {type(exc).__name__}', flush=True)
                if args.once: raise
            time.sleep(max(1, args.interval))


if __name__ == '__main__': main()
