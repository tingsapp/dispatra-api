from contextlib import contextmanager
from fastapi import APIRouter, Depends, Request
from app import auth
from app.database import session
from app.routes import OperationKey
from app.security import rate_limit
from .service import Principal, methods, setup_card
from .schemas import PaymentMethodsView, CardSetupInput, CardSetupView

router = APIRouter(prefix='/api/v1/companies/{slug}/shipper/payment-methods', tags=['Shipper payments'])


def principal(slug: str, request: Request):
    # Finish authentication's read transaction before a provider call starts.
    with contextmanager(session)() as db:
        user = auth.authenticated(request, db)
        auth.company_user(slug, user, db)
        auth.customer(user)
        return Principal(user.id, user.organization_id, user.shipper_id, slug)


@router.get('', response_model=PaymentMethodsView)
def payment_methods(actor: Principal = Depends(principal)):
    rate_limit('stripe-read:' + str(actor.id), 120)
    return methods(actor)


@router.post('/setup', response_model=CardSetupView)
def start_card_setup(data: CardSetupInput, idempotency_key: OperationKey, actor: Principal = Depends(principal)):
    rate_limit('stripe-setup:' + str(actor.id), 20)
    return setup_card(actor, idempotency_key)
