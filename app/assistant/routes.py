from typing import Annotated
from fastapi import APIRouter, Depends
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.security import rate_limit
from .schemas import (WorkspaceAssistantQuestion, AssistantReply, BriefingTotals, AssistantActionCommand, AssistantActionResult,
    AssistantChangeCommand, AssistantChangeResult)
from .service import ask, totals
from . import actions, changes

router = APIRouter(prefix='/api/v1/companies/{slug}/assistant', tags=['Dispatra AI'])


@router.post('/messages', response_model=AssistantReply)
def message(slug: str, data: WorkspaceAssistantQuestion, db: DB, user: Annotated[User, Depends(auth.dispatcher)]):
    rate_limit(f'assistant:{user.id}', 60)
    return ask(db, user, data)


@router.get('/briefing', response_model=BriefingTotals)
def briefing(slug: str, db: DB, user: Annotated[User, Depends(auth.dispatcher)]):
    rate_limit(f'assistant-briefing:{user.id}', 240)
    return totals(db, user)


@router.post('/actions/assign', response_model=AssistantActionResult)
def confirm_assignment(slug: str, data: AssistantActionCommand, db: DB, key: OperationKey, user: Annotated[User, Depends(auth.dispatcher)]):
    """Save an assignment the dispatcher confirmed in chat. The shared assignment command re-checks every rule."""
    rate_limit(f'assistant-action:{user.id}', 60)
    return actions.confirm(db, user, data, key)


@router.post('/actions/change', response_model=AssistantChangeResult)
def confirm_change(slug: str, data: AssistantChangeCommand, db: DB, key: OperationKey, user: Annotated[User, Depends(auth.dispatcher)]):
    """Save a record change the dispatcher confirmed in chat through its workspace command, as that dispatcher."""
    rate_limit(f'assistant-action:{user.id}', 60)
    return changes.confirm(db, user, data, key)
