from typing import Annotated
from fastapi import APIRouter, Depends
from app import auth
from app.models import User
from app.routes import DB
from app.security import rate_limit
from .schemas import AssistantQuestion, AssistantReply
from .service import ask

router = APIRouter(prefix='/api/v1/companies/{slug}/assistant', tags=['Dispatra AI'])


@router.post('/messages', response_model=AssistantReply)
def message(slug: str, data: AssistantQuestion, db: DB, user: Annotated[User, Depends(auth.dispatcher)]):
    rate_limit(f'assistant:{user.id}', 60)
    return ask(db, user, data)
