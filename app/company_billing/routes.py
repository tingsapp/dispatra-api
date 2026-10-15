from uuid import UUID
from typing import Annotated
from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, Header, Request, HTTPException, Query
from app.auth import dispatcher, platform
from app.routes import DB, OperationKey
from .schemas import CompanyPaymentView, CardSetupView, CardSetupConfirm, CardSetupRequest, CompanyPaymentHistory
from . import service, webhook, history

router = APIRouter(tags=['Company subscription payment methods'])
company = '/api/v1/companies/{slug}/billing/payment-method'
owner = '/api/v1/platform/organizations/{organization_id}/payment-method'


@router.get(company, response_model=CompanyPaymentView)
def company_payment(slug: str, db: DB, user=Depends(dispatcher)):
    return service.view(db, user.organization_id)


@router.post(company + '/setup', response_model=CardSetupView)
def company_setup(slug: str, data: CardSetupRequest, db: DB, idempotency_key: OperationKey, user=Depends(dispatcher)):
    return service.start(db, user, user.organization_id, idempotency_key, data.terms_version)


@router.post(company + '/confirm', response_model=CompanyPaymentView)
def company_confirm(slug: str, data: CardSetupConfirm, db: DB, user=Depends(dispatcher)):
    return service.complete(db, user.organization_id, data.session_id, user)


@router.post(company + '/refresh', response_model=CompanyPaymentView)
def company_refresh(slug: str, db: DB, user=Depends(dispatcher)):
    return service.refresh(db, user.organization_id, user)


@router.get(owner, response_model=CompanyPaymentView)
def owner_payment(organization_id: UUID, db: DB, user=Depends(platform)):
    return service.view(db, organization_id)


@router.post(owner + '/setup', response_model=CardSetupView)
def owner_setup(organization_id: UUID, data: CardSetupRequest, db: DB, idempotency_key: OperationKey, user=Depends(platform)):
    return service.start(db, user, organization_id, idempotency_key, data.terms_version)


@router.post(owner + '/confirm', response_model=CompanyPaymentView)
def owner_confirm(organization_id: UUID, data: CardSetupConfirm, db: DB, user=Depends(platform)):
    return service.complete(db, organization_id, data.session_id, user)


@router.post(owner + '/refresh', response_model=CompanyPaymentView)
def owner_refresh(organization_id: UUID, db: DB, user=Depends(platform)):
    return service.refresh(db, organization_id, user)


@router.post('/api/v1/billing/stripe/webhook', status_code=204)
async def stripe_webhook(request: Request, db: DB, stripe_signature: Annotated[str, Header(max_length=2048)] = ''):
    payload = await request.body()
    if len(payload) > 262144: raise HTTPException(413, 'Payment notification is too large.')
    event = webhook.verify(payload, stripe_signature)
    await run_in_threadpool(webhook.receive, db, event)


HistoryCursor = Annotated[str | None, Query(pattern=r'^ch_[a-zA-Z0-9]+$', max_length=255)]


@router.get('/api/v1/companies/{slug}/billing/payments', response_model=CompanyPaymentHistory)
def company_payments(slug: str, db: DB, user=Depends(dispatcher), after: HistoryCursor = None):
    return history.payments(db, user.organization_id, user, after)


@router.get('/api/v1/platform/organizations/{organization_id}/payments', response_model=CompanyPaymentHistory)
def owner_payments(organization_id: UUID, db: DB, user=Depends(platform), after: HistoryCursor = None):
    return history.payments(db, organization_id, user, after)
