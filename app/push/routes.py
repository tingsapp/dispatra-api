"""Native driver phones register their FCM token for the current signed-in session."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from app import auth
from app.models import User
from app.routes import DB
from app.schemas import Input
from app.security import digest
from . import service

router = APIRouter(prefix='/api/v1/companies/{slug}/driver', tags=['Driver push'])


class PushDeviceInput(Input):
    token: Annotated[str, Field(min_length=20, max_length=4096, pattern=r'^[A-Za-z0-9_:\-.]+$')]
    platform: Literal['ANDROID', 'IOS']


class PushDeviceRemove(Input):
    token: Annotated[str, Field(min_length=1, max_length=4096)]


@router.put('/push-device', status_code=204, response_class=Response)
def register(slug: str, data: PushDeviceInput, request: Request, db: DB, user: User = Depends(auth.driver)):
    """Idempotent: the same token re-registers to the current session."""
    service.register(db, user, digest(auth.request_token(request)), data)


@router.post('/push-device/remove', status_code=204, response_class=Response)
def unregister(slug: str, data: PushDeviceRemove, db: DB, user: User = Depends(auth.driver)):
    service.unregister(db, user, data)
