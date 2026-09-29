"""Internal lease/retry boundary for reliable event consumers.

Workers must set transaction-local tenant context before calling these functions.
Consumers refetch current entity state and use the event ID as the provider-side
idempotency key. A claimed event is never proof that an external action succeeded.
"""
from datetime import timedelta
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import or_, select
from app.models import OutboxEvent, now

LEASE = timedelta(minutes=5)


def claim(db, organization_id: UUID, worker_id: str, limit: int = 50, event_types: tuple[str, ...] | None = None):
    if not 1 <= limit <= 100 or not worker_id or len(worker_id) > 80:
        raise ValueError('Invalid worker claim parameters.')
    if event_types is not None and (not event_types or any(not name for name in event_types)):
        raise ValueError('Invalid event types.')
    observed_at = now()
    query = select(OutboxEvent).where(
        OutboxEvent.organization_id == organization_id,
        OutboxEvent.published_at.is_(None),
        OutboxEvent.available_at <= observed_at,
        or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < observed_at - LEASE),
    )
    if event_types is not None:
        query = query.where(OutboxEvent.event_type.in_(event_types))
    rows = db.scalars(query.order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
        .limit(limit).with_for_update(skip_locked=True)).all()
    for row in rows:
        row.attempts += 1
        row.claimed_at = observed_at
        row.claimed_by = worker_id
        row.last_error_code = None
    db.flush()
    return rows


def _owned(db, organization_id: UUID, event_id: UUID, worker_id: str):
    row = db.scalar(select(OutboxEvent).where(
        OutboxEvent.id == event_id, OutboxEvent.organization_id == organization_id,
        OutboxEvent.published_at.is_(None)).with_for_update())
    if not row or row.claimed_by != worker_id or row.claimed_at is None or row.claimed_at < now() - LEASE:
        raise HTTPException(409, 'Outbox event lease is unavailable or expired.')
    return row


def acknowledge(db, organization_id: UUID, event_id: UUID, worker_id: str):
    row = _owned(db, organization_id, event_id, worker_id)
    row.published_at = now()
    row.claimed_at = None
    row.claimed_by = None
    db.flush()


def retry(db, organization_id: UUID, event_id: UUID, worker_id: str, error_code: str):
    if not error_code or len(error_code) > 80 or not all(c.isalnum() or c in '._-' for c in error_code):
        raise ValueError('Use a short non-sensitive error code.')
    row = _owned(db, organization_id, event_id, worker_id)
    row.available_at = now() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 10)))
    row.last_error_code = error_code
    row.claimed_at = None
    row.claimed_by = None
    db.flush()
