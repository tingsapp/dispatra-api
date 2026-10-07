from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.security import rate_limit
from app.operations.schemas import AssignmentView
from . import service
from .schemas import DecisionApproval, DecisionView

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Dispatch agent'])
Dispatcher = Annotated[User, Depends(auth.dispatcher)]


@router.post('/orders/{identity}/dispatch-suggestions', response_model=DecisionView)
def suggest(slug: str, identity: UUID, db: DB, user: Dispatcher, refresh: bool = Query(False)):
    """Rank the drivers who can take this unassigned Order. A recent suggestion for the same Order version is reused unless `refresh`."""
    rate_limit(f'dispatch-suggest:{user.id}', 30)
    return service.suggest(db, user, identity, refresh)


@router.get('/orders/{identity}/dispatch-decisions', response_model=list[DecisionView])
def decisions(slug: str, identity: UUID, db: DB, user: Dispatcher):
    return service.history(db, user, identity)


@router.post('/dispatch-decisions/{identity}/approve', response_model=AssignmentView)
def approve(slug: str, identity: UUID, data: DecisionApproval, db: DB, key: OperationKey, user: Dispatcher):
    return service.approve(db, user, identity, data, key)
