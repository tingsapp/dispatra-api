from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.events.schemas import ReadAllView
from . import notifications
from .schemas import PlatformNotificationView, PlatformInbox, VersionCommand

router = APIRouter(prefix='/api/v1/platform/notifications', tags=['Platform administration'])
Owner = Annotated[User, Depends(auth.platform)]


@router.get('', response_model=PlatformInbox)
def list_notifications(db: DB, user: Owner, limit: int = Query(50, ge=1, le=200), before: UUID | None = None):
    return notifications.page(db, user, limit, before)


@router.post('/{identity}/read', response_model=PlatformNotificationView)
def read_notification(identity: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return notifications.mark_read(db, user, identity, data, idempotency_key)


@router.post('/read-all', response_model=ReadAllView)
def read_all_notifications(db: DB, idempotency_key: OperationKey, user: Owner):
    return notifications.mark_all_read(db, user, idempotency_key)
