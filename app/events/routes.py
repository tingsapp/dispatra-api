"""Client event feed and per-account notification inbox for dispatchers, Shippers and drivers."""
import os
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, tuple_, update
from app import auth
from app.models import User, now
from app.routes import DB, OperationKey
from app.services import audit
from app.operations.common import command, changed, version
from app.operations.schemas import Version
from . import feed
from .models import Notification
from .schemas import NotificationView, ReadAllView, SyncView

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Events and notifications'])
BATCH = 200


def poll_interval():
    """Server-chosen client poll delay; deployments tune it without client changes."""
    try: value = int(os.environ.get('SYNC_POLL_MS', '15000'))
    except ValueError: value = 15000
    return min(max(value, 1000), 300000)


def inbox(user):
    return select(Notification).where(Notification.organization_id == user.organization_id, Notification.recipient_user_id == user.id)


def unread(db, user):
    return db.scalar(select(func.count()).select_from(inbox(user).where(Notification.read_at.is_(None)).subquery()))


def page(db, user, limit, before=None):
    query = inbox(user)
    if before:
        anchor = db.scalar(inbox(user).where(Notification.id == before))
        if anchor is None: raise HTTPException(404, 'Notification not found.')
        query = query.where(tuple_(Notification.created_at, Notification.id) < (anchor.created_at, anchor.id))
    return db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)).all()


def mark_read(db, user, identity, data, key, view):
    def run():
        row = db.scalar(inbox(user).where(Notification.id == identity).with_for_update())
        if row is None: raise HTTPException(404, 'Notification not found.')
        if not row.read_at:
            version(row, data.version)
            row.read_at = now()
            changed(db, user, row, 'notification.read')
        return view.model_validate(row).model_dump(mode='json')
    return command(db, user, key, 'notification-read:' + str(identity), data.model_dump(), run)


@router.get('/sync', response_model=SyncView)
def sync(slug: str, db: DB, cursor: str | None = Query(None, max_length=42), user: User = Depends(auth.company_user)):
    rows, position, reset = feed.for_user(db, user, cursor, BATCH)
    return {'cursor': position, 'changes': [dict(row) for row in rows], 'unread': unread(db, user), 'reset': reset,
        'next_poll_ms': 0 if len(rows) == BATCH else poll_interval()}


@router.get('/notifications', response_model=list[NotificationView])
def notifications(slug: str, db: DB, limit: int = Query(50, ge=1, le=200), before: UUID | None = None, user: User = Depends(auth.company_user)):
    return page(db, user, limit, before)


@router.post('/notifications/{identity}/read', response_model=NotificationView)
def read_notification(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    return mark_read(db, user, identity, data, idempotency_key, NotificationView)


@router.post('/notifications/read-all', response_model=ReadAllView)
def read_all(slug: str, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    def run():
        count = db.execute(update(Notification).where(Notification.organization_id == user.organization_id,
            Notification.recipient_user_id == user.id, Notification.read_at.is_(None))
            .values(read_at=now(), version=Notification.version + 1, updated_at=now())).rowcount
        if count: audit(db, user, 'notification.read_all', user.id, user.organization_id)
        return {'updated': count}
    return command(db, user, idempotency_key, 'notification-read-all', {}, run)
