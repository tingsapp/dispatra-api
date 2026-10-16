"""Saved-card subscription purchase, using real restricted PostgreSQL and a Stripe test double."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from conftest import owner_engine, login, company, post, customer, HEADERS
from test_company_payments import stripe, start, ready, clear_card, BASE
from test_company_subscriptions import fleet
from app.main import app
from app.models import Organization
from app.company_billing import service, recurring
from app.company_billing.models import CompanyBilling

ENROLL = '/api/v1/companies/acme/billing/subscription'


def legacy_card(client, stripe, trial=False):
    login(client); org = company(client); clear_card(org['id'], trial)
    login(client, 'dispatch', 'acme', 'dispatcher')
    session = start(client); ready(stripe, session)
    checkout = stripe['sessions'][session]
    stripe['customers'][checkout['customer']].update(livemode=False, invoice_settings={'default_payment_method':'pm_card'})
    stripe['methods']['pm_card']['livemode'] = False
    identity = UUID(org['id'])
    with Session(owner_engine) as db, db.begin():
        row = db.get(CompanyBilling, identity)
        service.save_card(db, db.get(Organization, identity), row, stripe['methods']['pm_card'])
        row.setup_terms_version = row.setup_session = row.setup_nonce = row.consent_terms = None
    return org


def authorize(client, version=1, key=None):
    return post(client, ENROLL, {'consent':True, 'terms_version':version}, key)


def test_saved_card_stays_unsubscribed_until_explicit_purchase_then_worker_enrolls_once(client, stripe):
    org = legacy_card(client, stripe)
    fleet(org, 2)
    before = client.get(BASE).json()
    assert before['status'] == 'SAVED' and not before['recurring_authorized']
    assert not recurring.synchronize(org['id']) and not stripe['subscriptions']
    assert post(client, ENROLL, {'consent':False, 'terms_version':1}).status_code == 422
    assert post(client, ENROLL, {'consent':True, 'terms_version':1, 'monthly_price':'0'}).status_code == 422
    key = str(uuid4())
    response = authorize(client, key=key)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['recurring_authorized'] and saved['subscription_status'] == 'PENDING'
    assert authorize(client, key=key).json() == saved
    assert authorize(client).status_code == 200
    assert post(client, ENROLL, {'consent':True, 'terms_version':2}, key).status_code == 409
    assert not stripe['subscriptions'], 'HTTP enrollment queues the durable billing worker.'
    recurring.synchronize(org['id']); recurring.synchronize(org['id'])
    assert len(stripe['subscriptions']) == 1 and len(stripe['sessions']) == 1
    sub = stripe['subscriptions']['sub_1']
    assert sub['default_payment_method'] == 'pm_card' and sub['items']['data'][0]['quantity'] == 2
    assert client.get(BASE).json()['subscription_status'] == 'active'
    with Session(owner_engine) as db:
        row = db.get(CompanyBilling, UUID(org['id']))
        assert row.consent_terms['monthly_price'] == '10.00'
        assert row.consent_terms['interval'] == 'month' and row.consent_terms['actor_id']
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='subscription.company_recurring_consent'")) == 1
    login(client)
    revenue = client.get('/api/v1/platform/billing/revenue').json()
    assert revenue['active_subscriptions'] == 1 and revenue['currencies'][0]['monthly_recurring_revenue'] == '20.00'


def test_saved_card_purchase_preserves_original_trial(client, stripe):
    org = legacy_card(client, stripe, trial=True)
    assert authorize(client).status_code == 200
    recurring.synchronize(org['id'])
    with Session(owner_engine) as db:
        cutoff = db.get(Organization, UUID(org['id'])).trial_ends_at
        assert stripe['subscriptions']['sub_1']['trial_end'] == int(cutoff.timestamp())
    assert client.get(BASE).json()['subscription_status'] == 'trialing'
    login(client)
    assert client.get('/api/v1/platform/billing/revenue').json()['active_subscriptions'] == 0


@pytest.mark.parametrize('change', ['terms', 'unset_price', 'expired', 'foreign_card', 'changed_default', 'live_card', 'deleted_customer', 'paused', 'cancelled'])
def test_stale_invalid_or_blocked_purchase_never_authorizes_or_charges(client, stripe, change):
    org = legacy_card(client, stripe)
    if change in {'terms', 'unset_price', 'paused', 'cancelled'}:
        fields = {'terms':'version=version+1', 'unset_price':'monthly_price=NULL', 'paused':"status='PAUSED'", 'cancelled':"status='CANCELLED'"}
        with owner_engine.begin() as db:
            db.execute(text('UPDATE company_subscriptions SET '+fields[change]+' WHERE organization_id=:id'), {'id':org['id']})
    if change == 'expired': stripe['methods']['pm_card']['card']['exp_year'] = 2020
    if change == 'foreign_card': stripe['methods']['pm_card']['customer'] = 'cus_foreign'
    if change == 'live_card': stripe['methods']['pm_card']['livemode'] = True
    customer_record = next(iter(stripe['customers'].values()))
    if change == 'changed_default': customer_record['invoice_settings']['default_payment_method'] = 'pm_other'
    if change == 'deleted_customer': customer_record['deleted'] = True
    assert authorize(client).status_code == 409
    with Session(owner_engine) as db:
        assert db.get(CompanyBilling, UUID(org['id'])).consented_at is None
        assert not db.get(Organization, UUID(org['id'])).billing_enrolled
    assert not stripe['subscriptions']


def test_enrollment_remains_dispatcher_only_and_tenant_scoped(client, stripe):
    org = legacy_card(client, stripe, trial=True)
    assert post(client, '/api/v1/companies/other/billing/subscription', {'consent':True,'terms_version':1}).status_code == 404
    login(client)
    assert authorize(client).status_code == 404
    login(client, 'dispatch', 'acme', 'dispatcher'); customer(client)
    login(client, 'customer', 'acme', 'customer')
    assert authorize(client).status_code == 403
    assert not stripe['subscriptions']


def test_competing_saved_card_purchases_share_one_consent_and_subscription(client, stripe):
    org = legacy_card(client, stripe)
    cookies = dict(client.cookies)
    def purchase(_):
        with TestClient(app, headers=HEADERS, cookies=cookies) as sender:
            return authorize(sender).status_code
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(purchase, range(2))) == [200, 200]
    recurring.synchronize(org['id'])
    assert len(stripe['subscriptions']) == 1
    with Session(owner_engine) as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='subscription.company_recurring_consent'")) == 1


def test_provider_unavailability_does_not_record_consent(client, stripe, monkeypatch):
    from fastapi import HTTPException
    from app.company_billing import provider
    org = legacy_card(client, stripe)
    def unavailable(*args, **kwargs):
        raise HTTPException(503, 'Unable to reach payment setup. Please try again.')
    monkeypatch.setattr(provider, 'call', unavailable)
    assert authorize(client).status_code == 503
    with Session(owner_engine) as db:
        assert db.get(CompanyBilling, UUID(org['id'])).consented_at is None
        assert not db.get(Organization, UUID(org['id'])).billing_enrolled
