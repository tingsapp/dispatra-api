from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.schemas import OrganizationCreate, OrganizationView
from . import service, queries
from .schemas import (CompanySummary, CompanyDetail, CompanyUpdate, VersionCommand, DispatcherView, DispatcherCredentialView,
    DispatcherCreate, DispatcherUpdate, SessionRevocation, AuditEntry)

# Every endpoint requires a ADMIN session; company roles receive 403.
router = APIRouter(prefix='/api/v1/platform/organizations', tags=['Platform administration'])
Owner = Annotated[User, Depends(auth.platform)]


@router.get('', response_model=list[CompanySummary])
def list_companies(db: DB, user: Owner, search: Annotated[str | None, Query(max_length=100)] = None,
                   status: Literal['ACTIVE', 'SUSPENDED'] | None = None, after: UUID | None = None, limit: int = Query(50, ge=1, le=100)):
    return queries.companies(db, search, status, after, limit)


@router.post('', response_model=OrganizationView, status_code=201)
def create_company(data: OrganizationCreate, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.create_organization(db, user, data, idempotency_key)


@router.get('/{organization_id}', response_model=CompanyDetail)
def get_company(organization_id: UUID, db: DB, user: Owner):
    return queries.company_detail(db, service.company(db, organization_id))


@router.patch('/{organization_id}', response_model=CompanyDetail)
def update_company(organization_id: UUID, data: CompanyUpdate, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.update_company(db, user, organization_id, data, idempotency_key)


@router.post('/{organization_id}/suspend', response_model=CompanyDetail)
def suspend_company(organization_id: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.set_company_status(db, user, organization_id, False, data, idempotency_key)


@router.post('/{organization_id}/activate', response_model=CompanyDetail)
def activate_company(organization_id: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.set_company_status(db, user, organization_id, True, data, idempotency_key)


@router.get('/{organization_id}/audit', response_model=list[AuditEntry])
def company_audit(organization_id: UUID, db: DB, user: Owner, limit: int = Query(50, ge=1, le=100)):
    return queries.audit_history(db, service.company(db, organization_id).id, limit)


@router.get('/{organization_id}/dispatchers', response_model=list[DispatcherView])
def list_dispatchers(organization_id: UUID, db: DB, user: Owner):
    return queries.dispatchers(db, service.company(db, organization_id).id)


@router.post('/{organization_id}/dispatchers', response_model=DispatcherCredentialView, status_code=201)
def create_dispatcher(organization_id: UUID, data: DispatcherCreate, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.create_dispatcher(db, user, organization_id, data, idempotency_key)


@router.patch('/{organization_id}/dispatchers/{user_id}', response_model=DispatcherView)
def update_dispatcher(organization_id: UUID, user_id: UUID, data: DispatcherUpdate, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.update_dispatcher(db, user, organization_id, user_id, data, idempotency_key)


@router.post('/{organization_id}/dispatchers/{user_id}/activate', response_model=DispatcherView)
def activate_dispatcher(organization_id: UUID, user_id: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.set_dispatcher_active(db, user, organization_id, user_id, True, data, idempotency_key)


@router.post('/{organization_id}/dispatchers/{user_id}/deactivate', response_model=DispatcherView)
def deactivate_dispatcher(organization_id: UUID, user_id: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.set_dispatcher_active(db, user, organization_id, user_id, False, data, idempotency_key)


@router.post('/{organization_id}/dispatchers/{user_id}/reset-password', response_model=DispatcherCredentialView)
def reset_dispatcher_password(organization_id: UUID, user_id: UUID, data: VersionCommand, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.reset_dispatcher_password(db, user, organization_id, user_id, data, idempotency_key)


@router.post('/{organization_id}/dispatchers/{user_id}/revoke-sessions', response_model=SessionRevocation)
def revoke_dispatcher_sessions(organization_id: UUID, user_id: UUID, db: DB, idempotency_key: OperationKey, user: Owner):
    return service.revoke_dispatcher_sessions(db, user, organization_id, user_id, idempotency_key)
