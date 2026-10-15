from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.models import Organization, AuditEvent, now
from app.security import rate_limit
from app.services import once
from .models import CompanyBilling
from .schemas import CompanyPaymentView
from . import provider, recurring


def organization(db, identity, lock=False):
    query = select(Organization).where(Organization.id == identity)
    org = db.scalar(query.with_for_update() if lock else query)
    if not org: raise HTTPException(404, 'Company not found.')
    return org


def record(db, identity):
    db.execute(insert(CompanyBilling).values(organization_id=identity).on_conflict_do_nothing())
    return db.scalar(select(CompanyBilling).where(CompanyBilling.organization_id == identity).with_for_update())


def view(db, identity):
    org = organization(db, identity)
    row = db.get(CompanyBilling, identity)
    saved = bool(org.payment_method_valid_until and org.payment_method_valid_until > now())
    agreement, count = recurring.terms(db, identity)
    return CompanyPaymentView(monthly_price=agreement.monthly_price, currency=agreement.currency,
        vehicle_count=count, monthly_total=agreement.monthly_price * count if agreement.monthly_price is not None else None,
        terms_version=agreement.version, starts_at=recurring.starts(org, agreement),
        subscription_status=(agreement.status if agreement.status in {'PAUSED','CANCELLED'} else (row.subscription_status or 'PENDING')) if row and row.consented_at else agreement.status,
        period_end=row.period_end if row else None, invoice_url=row.invoice_url if row else None,
        sync_error=row.sync_error if row else False, status='SAVED' if saved else 'EXPIRED' if row and row.last4 else 'MISSING',
        required=org.payment_required, trial_ends_at=org.trial_ends_at, setup_available=provider.available(),
        **({field: getattr(row, field) for field in ('brand', 'last4', 'exp_month', 'exp_year')} if row else {}))


def start(db, actor, identity, key, terms_version):
    return once(db, actor, key, 'company-recurring-setup:' + str(identity),
        {'consent':True, 'terms_version':terms_version}, lambda: _start(db, actor, identity, key, terms_version))


def _start(db, actor, identity, key, terms_version):
    if not provider.available(): raise HTTPException(503, 'Payment setup is unavailable. Contact Dispatra support.')
    rate_limit('card-setup:' + str(actor.id), 20)
    org = organization(db, identity, True)
    row = record(db, identity)
    agreement, _ = recurring.terms(db, identity)
    if agreement.version != terms_version: raise HTTPException(409, 'Subscription terms changed. Reload and review them before continuing.')
    if agreement.monthly_price is None: raise HTTPException(409, 'Contact Dispatra to set your subscription rate before adding a card.')
    # All retries use Stripe's same payload/key, including after an uncertain transaction.
    if not row.stripe_customer:
        customer = provider.call('POST', 'customers', {'metadata[organization_id]': str(identity)}, 'dispatra-customer:' + str(identity))
        row.stripe_customer = customer['id']
    nonce = uuid5(NAMESPACE_URL, f'dispatra-card:{identity}:{actor.id}:{key}').hex
    base = provider.origin()
    path = f'/admin/companies/{identity}/subscription' if actor.role == 'ADMIN' else f'/{org.slug}/subscription'
    params = {'mode': 'setup', 'customer': row.stripe_customer, 'payment_method_types[0]': 'card',
        'success_url': base + path + '?card_setup={CHECKOUT_SESSION_ID}', 'cancel_url': base + path + '?card_setup=cancelled',
        'metadata[organization_id]': str(identity), 'metadata[setup_nonce]': nonce,
        'setup_intent_data[metadata][organization_id]': str(identity), 'setup_intent_data[metadata][setup_nonce]': nonce}
    checkout = provider.call('POST', 'checkout/sessions', params, 'dispatra-card:' + nonce)
    url = provider.checkout_url(checkout.get('url'))
    if row.setup_nonce != nonce:
        db.add(AuditEvent(actor_id=actor.id, organization_id=identity, entity_id=identity, action='subscription.company_card_consent'))
    row.setup_session, row.setup_nonce = checkout['id'], nonce
    row.setup_terms_version = terms_version
    if not row.consented_at:
        row.consent_terms = {'terms_version':agreement.version, 'monthly_price':str(agreement.monthly_price),
            'currency':agreement.currency, 'interval':'month', 'unit':'non_archived_vehicle',
            'trial_ends_at':org.trial_ends_at.isoformat() if org.trial_ends_at else None,
            'actor_id':str(actor.id), 'accepted_at':now().isoformat()}
    db.flush()
    return {'url': url}


def expires(card):
    year, month = int(card['exp_year']), int(card['exp_month'])
    return datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)


def save_card(db, org, row, method, actor=None):
    previous = row.payment_method
    valid = method and method.get('type') == 'card' and method.get('customer') == row.stripe_customer
    card = method['card'] if valid else None
    row.payment_method = method['id'] if valid else None
    for field in ('brand', 'last4', 'exp_month', 'exp_year'):
        setattr(row, field, card[field] if card else None)
    org.payment_method_valid_until = expires(card) if card else None
    row.updated_at = now()
    if previous != row.payment_method:
        db.add(AuditEvent(actor_id=actor.id if actor else None, organization_id=org.id, entity_id=org.id,
            action='subscription.company_payment_saved' if valid else 'subscription.company_payment_removed'))
    db.flush()


def complete(db, identity, session_id, actor=None):
    if actor: rate_limit('card-confirm:' + str(actor.id), 30)
    org = organization(db, identity, True)
    row = record(db, identity)
    if session_id == row.completed_session: return view(db, identity)
    if session_id != row.setup_session: raise HTTPException(409, 'Payment setup has changed. Start again or refresh the payment method.')
    checkout = provider.call('GET', 'checkout/sessions/' + session_id)
    if checkout.get('mode') != 'setup' or checkout.get('status') != 'complete' or checkout.get('customer') != row.stripe_customer or checkout.get('metadata') != {'organization_id': str(identity), 'setup_nonce': row.setup_nonce}:
        raise HTTPException(409, 'Card setup is not complete. Please finish adding your card.')
    intent_id = checkout.get('setup_intent')
    if not isinstance(intent_id, str) or not intent_id.startswith('seti_'): raise HTTPException(409, 'Card setup is not complete.')
    intent = provider.call('GET', 'setup_intents/' + intent_id)
    if intent.get('status') != 'succeeded' or intent.get('customer') != row.stripe_customer or intent.get('metadata') != checkout['metadata']:
        raise HTTPException(409, 'Card setup could not be verified.')
    method = provider.call('GET', 'payment_methods/' + intent['payment_method'])
    if method.get('customer') != row.stripe_customer or method.get('type') != 'card' or expires(method['card']) <= now():
        raise HTTPException(409, 'Add a valid card for this company.')
    agreement, _ = recurring.terms(db, identity)
    if not row.consented_at and agreement.version != row.setup_terms_version:
        raise HTTPException(409, 'Subscription terms changed. Review the updated rate and start setup again.')
    provider.call('POST', 'customers/' + row.stripe_customer, {'invoice_settings[default_payment_method]': method['id']}, 'dispatra-default:' + row.setup_nonce)
    save_card(db, org, row, method, actor)
    if not row.consented_at:
        row.consented_at = now()
        db.add(AuditEvent(actor_id=actor.id if actor else None, organization_id=identity, entity_id=identity,
            action='subscription.company_recurring_consent'))
    org.billing_enrolled = True
    recurring.request_sync(row)
    row.completed_session, row.setup_session, row.setup_nonce = session_id, None, None
    db.flush()
    return view(db, identity)


def refresh(db, identity, actor=None):
    if actor: rate_limit('card-refresh:' + str(actor.id), 30)
    org = organization(db, identity, True)
    row = record(db, identity)
    if row.stripe_customer:
        customer = provider.call('GET', 'customers/' + row.stripe_customer)
        method_id = (customer.get('invoice_settings') or {}).get('default_payment_method') if not customer.get('deleted') else None
        method = provider.call('GET', 'payment_methods/' + method_id) if method_id else None
        save_card(db, org, row, method, actor)
        recurring.request_sync(row)
    return view(db, identity)
