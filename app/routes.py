from uuid import UUID
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from . import auth, services
from .database import session
from .models import User, LoginSession
from .operations.models import Shipper
from .schemas import (Login, AccountView, CustomerCreate,
                      CustomerAccessView, CustomerView, ProfileUpdate, PasswordChange, PasswordReset)
from .security import digest

router = APIRouter(prefix='/api/v1')
DB = Annotated[Session, Depends(session)]
OperationKey = Annotated[str, Header(alias='Idempotency-Key', min_length=16, max_length=80, pattern=r'^[a-zA-Z0-9-]+$')]

@router.post('/auth/login', response_model=AccountView)
def login(data: Login, request: Request, response: Response, db: DB):
    return auth.login(db, data, request, response)

@router.get('/auth/me', response_model=AccountView)
def me(db: DB, user: User = Depends(auth.authenticated)):
    return auth.account_view(db, user)

@router.post('/auth/logout', status_code=204)
def logout(request: Request, response: Response, db: DB):
    token = request.cookies.get(auth.COOKIE)
    if token:
        login_session = db.get(LoginSession,digest(token))
        if login_session:
            from .database import context
            context(db,login_session.organization_id)
            account = db.get(User,login_session.user_id)
            if account and account.driver_id:
                from .operations.execution import close_duty_on_logout
                close_duty_on_logout(db,account)
            db.delete(login_session)
    response.delete_cookie(auth.COOKIE, path='/', secure=auth.SECURE, httponly=True, samesite='strict')

@router.post('/auth/password', status_code=204)
def password(data: PasswordChange, response: Response, db: DB, user: User = Depends(auth.authenticated)):
    auth.change_password(db, user, data, response)

@router.get('/companies/{slug}/customers', response_model=list[CustomerAccessView])
def customers(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50, ge=1, le=100), user: User = Depends(auth.dispatcher)):
    query = select(Shipper, User.login_id).join(User, User.shipper_id == Shipper.id).where(Shipper.organization_id == user.organization_id).order_by(Shipper.id).limit(limit)
    if after: query = query.where(Shipper.id > after)
    return [{**CustomerView.model_validate(c).model_dump(), 'login_id': login} for c, login in db.execute(query)]

@router.post('/companies/{slug}/customers', response_model=CustomerAccessView, status_code=201)
def create_customer(slug: str, data: CustomerCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return services.create_customer(db, user, data, idempotency_key)

@router.post('/companies/{slug}/customers/{customer_id}/password', status_code=204)
def reset_password(slug: str, customer_id: UUID, data: PasswordReset, db: DB, user: User = Depends(auth.dispatcher)):
    services.reset_customer_password(db, user, customer_id, data.password)

@router.get('/companies/{slug}/profile', response_model=CustomerView)
def profile(slug: str, db: DB, user: User = Depends(auth.customer)):
    return db.scalar(select(Shipper).where(Shipper.id == user.shipper_id, Shipper.organization_id == user.organization_id))

@router.patch('/companies/{slug}/profile', response_model=CustomerView)
def update_profile(slug: str, data: ProfileUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.customer)):
    return services.update_profile(db, user, data, idempotency_key)
