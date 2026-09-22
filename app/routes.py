from uuid import UUID
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from . import auth, services
from .database import session
from .models import Organization, Customer, User, LoginSession
from .schemas import (Login, AccountView, OrganizationCreate, OrganizationView, CustomerCreate,
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
    if token: db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(token)))
    response.delete_cookie(auth.COOKIE, path='/', secure=auth.SECURE, httponly=True, samesite='strict')

@router.post('/auth/password', status_code=204)
def password(data: PasswordChange, response: Response, db: DB, user: User = Depends(auth.authenticated)):
    auth.change_password(db, user, data, response)

@router.get('/platform/organizations', response_model=list[OrganizationView])
def organizations(db: DB, after: UUID | None = None, limit: int = Query(50, ge=1, le=100), user: User = Depends(auth.platform)):
    query = select(Organization).order_by(Organization.id).limit(limit)
    if after: query = query.where(Organization.id > after)
    return db.scalars(query).all()

@router.post('/platform/organizations', response_model=OrganizationView, status_code=201)
def organization(data: OrganizationCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.platform)):
    return services.create_organization(db, user, data, idempotency_key)

@router.get('/companies/{slug}/customers', response_model=list[CustomerAccessView])
def customers(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50, ge=1, le=100), user: User = Depends(auth.dispatcher)):
    query = select(Customer, User.login_id).join(User, User.customer_id == Customer.id).where(Customer.organization_id == user.organization_id).order_by(Customer.id).limit(limit)
    if after: query = query.where(Customer.id > after)
    return [{**CustomerView.model_validate(c).model_dump(), 'login_id': login} for c, login in db.execute(query)]

@router.post('/companies/{slug}/customers', response_model=CustomerAccessView, status_code=201)
def create_customer(slug: str, data: CustomerCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return services.create_customer(db, user, data, idempotency_key)

@router.post('/companies/{slug}/customers/{customer_id}/password', status_code=204)
def reset_password(slug: str, customer_id: UUID, data: PasswordReset, db: DB, user: User = Depends(auth.dispatcher)):
    services.reset_customer_password(db, user, customer_id, data.password)

@router.get('/companies/{slug}/profile', response_model=CustomerView)
def profile(slug: str, db: DB, user: User = Depends(auth.customer)):
    return db.scalar(select(Customer).where(Customer.id == user.customer_id, Customer.organization_id == user.organization_id))

@router.patch('/companies/{slug}/profile', response_model=CustomerView)
def update_profile(slug: str, data: ProfileUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.customer)):
    return services.update_profile(db, user, data, idempotency_key)
