"""Short database transactions around external Stripe calls; no card data stored."""
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4
from urllib.parse import urlsplit
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import Organization, User, now
from app.services import audit, once
from app.security import digest
from app.operations.models import Shipper
from app.operations.common import record
from .models import StripeConnection, StripeCustomer
from . import provider


@dataclass(frozen=True)
class Principal:
    id: UUID
    organization_id: UUID
    shipper_id: UUID
    slug: str


@contextmanager
def transaction(actor):
    with Session(engine, expire_on_commit=False) as db, db.begin():
        context(db, actor.organization_id, shipper_id=actor.shipper_id)
        user = db.get(User, actor.id)
        shipper = customer = record(db, Shipper, actor, actor.shipper_id)
        organization = db.get(Organization, actor.organization_id)
        if (not user or not user.active or user.role != 'SHIPPER' or user.shipper_id != actor.shipper_id
            or not organization or not organization.active or customer.status == 'INACTIVE' or shipper.archived_at):
            raise HTTPException(403, 'This shipper account is not active.')
        yield db, shipper


def connection(db, actor):
    return db.scalar(select(StripeConnection).where(StripeConnection.organization_id == actor.organization_id, StripeConnection.livemode == provider.livemode()))


def customer_row(db, actor, account, lock=False):
    query = select(StripeCustomer).where(StripeCustomer.organization_id == actor.organization_id,
        StripeCustomer.shipper_id == actor.shipper_id, StripeCustomer.account_id == account,
        StripeCustomer.livemode == provider.livemode())
    return db.scalar(query.with_for_update() if lock else query)


def methods(actor):
    with transaction(actor) as (db, shipper):
        link = connection(db, actor)
        enabled = bool(link and provider.configured())
        row = customer_row(db, actor, link.account_id) if enabled else None
        account = link.account_id if link else None
        customer_id = row.customer_id if row else None
        result = {'configured': enabled, 'test_mode': not provider.livemode(), 'terms': shipper.terms, 'cards': []}
    if not customer_id: return result
    after = None
    while True:
        params = {'type': 'card', 'limit': 100}
        if after: params['starting_after'] = after
        page = provider.request('GET', f'customers/{customer_id}/payment_methods', account=account, data=params)
        for method in page.get('data', []):
            if method.get('customer') != customer_id or method.get('type') != 'card': continue
            card = method['card']
            result['cards'].append({ 'id': method['id'], 'brand': card['brand'], 'last4': card['last4'],
                'exp_month': card['exp_month'], 'exp_year': card['exp_year'] })
        if not page.get('has_more'): break
        if not page.get('data') or page['data'][-1]['id'] == after: raise HTTPException(503, 'Stripe returned incomplete card information.')
        after = page['data'][-1]['id']
    return result


def ensure_customer(actor):
    with transaction(actor) as (db, shipper):
        link = connection(db, actor)
        if not link or not provider.configured(): raise HTTPException(409, 'Credit card setup is not configured for this company.')
        account = link.account_id
        # Reserve one durable idempotency identity before calling Stripe.
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))'), {'scope': 'stripe-customer:' + str(actor.shipper_id)})
        row = customer_row(db, actor, account, True)
        if row is None:
            row = StripeCustomer(id=uuid4(), organization_id=actor.organization_id, shipper_id=actor.shipper_id,
                account_id=account, livemode=provider.livemode())
            db.add(row); db.flush()
        if row.customer_id: return account, row.customer_id
        if row.created_at < now() - timedelta(hours=23):
            raise HTTPException(409, 'An earlier card setup needs reconciliation. Please contact your dispatch company.')
        reservation = row.id
    remote = provider.request('POST', 'customers', account=account, key='shipper-customer:' + str(reservation), data={
        'metadata[dispatra_company]': str(actor.organization_id), 'metadata[dispatra_shipper]': str(actor.shipper_id)})
    customer_id = remote.get('id', '')
    if not re.fullmatch(r'cus_[A-Za-z0-9]+', customer_id): raise HTTPException(503, 'Stripe returned an invalid customer.')
    with transaction(actor) as (db, shipper):
        row = customer_row(db, actor, account, True)
        if row.customer_id and row.customer_id != customer_id: raise HTTPException(409, 'Card setup requires reconciliation.')
        if not row.customer_id:
            row.customer_id = customer_id
            row.version += 1
            audit(db, actor, 'shipper.stripe_customer_created', actor.shipper_id, actor.organization_id)
    return account, customer_id


def return_url(actor):
    origin = os.environ.get('PUBLIC_WEB_URL', '').rstrip('/')
    parsed = urlsplit(origin)
    if not origin or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or not (
        parsed.scheme == 'https' or parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}):
        raise HTTPException(503, 'Card setup return URL is not configured.')
    return origin + f'/{actor.slug}/shipper-portal/payment-methods'


def setup_card(actor, key):
    target = return_url(actor)
    account, customer_id = ensure_customer(actor)
    result = provider.request('POST', 'checkout/sessions', account=account,
        key='shipper-setup:' + digest(f'{actor.id}:{key}'), data={
            'mode': 'setup', 'customer': customer_id, 'payment_method_types[0]': 'card',
            'setup_intent_data[usage]': 'on_session',
            'setup_intent_data[metadata][dispatra_shipper]': str(actor.shipper_id),
            'success_url': target + '?card_setup=returned', 'cancel_url': target + '?card_setup=cancelled',
        })
    url = result.get('url', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'checkout.stripe.com' or parsed.username or parsed.password:
        raise HTTPException(503, 'Stripe returned an invalid setup link.')
    with transaction(actor) as (db, shipper):
        def logged():
            audit(db, actor, 'shipper.card_setup_requested', actor.shipper_id, actor.organization_id)
            return {'session_id': result['id']}
        once(db, actor, key, 'stripe-card-setup', {'consent': True}, logged)
    # Never persist the hosted session URL or mistake a redirect for a saved card.
    return {'url': url}
