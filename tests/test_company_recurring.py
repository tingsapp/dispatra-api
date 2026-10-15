"""Recurring lifecycle with actual PostgreSQL leases, tenant boundaries and HTTP consent."""
from uuid import UUID
from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from sqlalchemy import text, select
from sqlalchemy.orm import Session
from conftest import owner_engine, login, company, post
from test_company_payments import stripe, start, ready, clear_card, BASE, notification
from app.models import Organization, CompanySubscription, now
from app.company_billing import recurring, provider
from app.company_billing.models import CompanyBilling


def enroll(client, stripe, trial=False):
    login(client); org=company(client); clear_card(org['id'], trial)
    login(client,'dispatch','acme','dispatcher')
    session=start(client); ready(stripe,session)
    assert post(client,BASE+'/confirm',{'session_id':session}).status_code==200
    return org


def test_trial_preserves_cutoff_and_pending_then_paid_access(client,stripe):
    org=enroll(client,stripe,True)
    recurring.synchronize(org['id'])
    sub=stripe['subscriptions']['sub_1']
    with Session(owner_engine) as db:
        saved=db.get(Organization,UUID(org['id']))
        assert sub['trial_end']==int(saved.trial_ends_at.timestamp())
        assert saved.active and saved.billing_enrolled
        assert db.get(CompanyBilling,UUID(org['id'])).consent_terms['monthly_price']=='10.00'
    assert sub['status']=='trialing'
    assert len(stripe['subscriptions'])==1


def test_payment_failure_authentication_recovery_replay_and_remote_cancel(client,stripe):
    org=enroll(client,stripe)
    recurring.synchronize(org['id'])
    sub=stripe['subscriptions']['sub_1']; invoice=stripe['invoices'][sub['latest_invoice']]
    assert not client.get(BASE).json()['required']
    sub['status']='past_due';invoice['status']='open'
    event={'type':'invoice.payment_failed','data':{'object':{'customer':sub['customer'],'id':invoice['id'],'status':'paid'}}}
    assert notification(client,event).status_code==204
    recurring.synchronize(org['id'])
    assert client.get(BASE).json()['required']
    assert client.get(BASE).json()['invoice_url']=='https://invoice.stripe.com/i/test'
    assert client.get('/api/v1/companies/acme/settings').status_code==403
    # Old snapshots cannot re-block a paid renewal.
    invoice['status']='paid';sub['status']='active'
    notification(client,event);recurring.synchronize(org['id'])
    assert not client.get(BASE).json()['required']
    sub['status']='canceled';recurring.synchronize(org['id'])
    assert client.get(BASE).json()['required']
    recurring.synchronize(org['id'])
    assert len(stripe['subscriptions'])==1
    with Session(owner_engine) as db: assert db.get(CompanySubscription,UUID(org['id'])).status=='CANCELLED'


def test_replacement_keeps_subscription_and_retry_after_lost_publish_cannot_duplicate(client,stripe,monkeypatch):
    org=enroll(client,stripe)
    real_publish=recurring.publish
    def interrupted(*args,**kwargs): raise RuntimeError('simulated process loss after remote create')
    monkeypatch.setattr(recurring,'publish',interrupted)
    with pytest.raises(RuntimeError): recurring.synchronize(org['id'])
    with owner_engine.begin() as db: db.execute(text('UPDATE company_billing SET lease_until=now()-interval \'1 second\''))
    monkeypatch.setattr(recurring,'publish',real_publish)
    recurring.synchronize(org['id'])
    assert len(stripe['subscriptions'])==1
    second=start(client);ready(stripe,second,'pm_new')
    assert post(client,BASE+'/confirm',{'session_id':second}).status_code==200
    recurring.synchronize(org['id'])
    assert len(stripe['subscriptions'])==1
    assert stripe['subscriptions']['sub_1']['default_payment_method']=='pm_new'


def test_leases_prevent_competing_workers_and_transport_retry_keeps_frozen_payload(client,stripe,monkeypatch):
    org=enroll(client,stripe)
    with ThreadPoolExecutor(2) as pool: claims=list(pool.map(recurring.claim,[org['id'],org['id']]))
    assert sum(c is not None for c in claims)==1
    token,data=next(c for c in claims if c)
    recurring.publish(org['id'],token,data,failed=True)
    again=recurring.claim(org['id'])
    assert again[1]==data
    recurring.publish(org['id'],again[0],data,recurring.remote(data))
    assert len(stripe['subscriptions'])==1


def test_owner_pause_resume_price_quantity_and_currency_no_proration(client,stripe):
    org=enroll(client,stripe);recurring.synchronize(org['id'])
    sub=stripe['subscriptions']['sub_1']
    from test_company_subscriptions import fleet
    fleet(org,2);fleet(org,1,active=False);fleet(org,4,archived=True)
    recurring.synchronize(org['id'])
    assert sub['items']['data'][0]['quantity']==3
    assert client.get(BASE).json()['monthly_total']=='30.00'
    login(client)
    path=f"/api/v1/platform/organizations/{org['id']}/subscription"
    def edit(**values):
        current=client.get(path).json()
        body={k:current[k] for k in ('status','monthly_price','currency','starts_on','version')};body.update(values)
        result=client.patch(path,json=body,headers={'Idempotency-Key':str(__import__('uuid').uuid4())})
        assert result.status_code==200,result.text
    edit(monthly_price='15.50')
    recurring.synchronize(org['id'])
    assert sub['items']['data'][0]['price']['unit_amount_decimal']=='1550'
    edit(status='PAUSED');recurring.synchronize(org['id'])
    assert sub['status']=='canceled'
    edit(status='ACTIVE');recurring.synchronize(org['id'])
    assert len(stripe['subscriptions'])==2
    assert stripe['subscriptions']['sub_2']['trial_end']==sub['items']['data'][0]['current_period_end']
    edit(currency='USD');recurring.synchronize(org['id'])
    assert len([s for s in stripe['subscriptions'].values() if s['status'] not in recurring.TERMINAL])==1
    assert stripe['subscriptions']['sub_3']['currency']=='usd'
    assert all(data.get('proration_behavior')=='none' for method,path,data,key in stripe['calls'] if method=='POST' and path.startswith('subscriptions/'))


def test_consent_version_conflict_no_price_and_mode_guard(client,stripe,monkeypatch):
    login(client);company(client);login(client,'dispatch','acme','dispatcher')
    assert post(client,BASE+'/setup',{'consent':True,'terms_version':999}).status_code==409
    with owner_engine.begin() as db: db.execute(text('UPDATE company_subscriptions SET monthly_price=NULL'))
    assert post(client,BASE+'/setup',{'consent':True,'terms_version':1}).status_code==409
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_live_forbidden')
    assert not provider.available()
    assert post(client,BASE+'/setup',{'consent':True,'terms_version':1}).status_code==503


def test_zero_price_to_paid_preserves_renewal_date_without_immediate_charge(client,stripe):
    org=enroll(client,stripe)
    with owner_engine.begin() as db: db.execute(text('UPDATE company_subscriptions SET monthly_price=0 WHERE organization_id=:id'), {'id':org['id']})
    recurring.synchronize(org['id'])
    first=stripe['subscriptions']['sub_1']
    with owner_engine.begin() as db: db.execute(text('UPDATE company_subscriptions SET monthly_price=20,version=version+1 WHERE organization_id=:id'), {'id':org['id']})
    recurring.synchronize(org['id'])
    second=stripe['subscriptions']['sub_2']
    assert first['status']=='canceled' and second['status']=='trialing'
    assert second['trial_end']==first['items']['data'][0]['current_period_end']
    assert len([row for row in stripe['subscriptions'].values() if row['status'] not in recurring.TERMINAL])==1


def test_saved_future_start_is_preserved_when_initial_agreement_enrolls(client,stripe):
    org=enroll(client,stripe,True)
    start=(now()+timedelta(days=10)).date()
    with owner_engine.begin() as db:
        db.execute(text('UPDATE company_subscriptions SET starts_on=:start WHERE organization_id=:id'), {'start':start,'id':org['id']})
    recurring.synchronize(org['id'])
    assert stripe['subscriptions']['sub_1']['trial_end']==int(__import__('datetime').datetime.combine(start,__import__('datetime').datetime.min.time(),__import__('datetime').timezone.utc).timestamp())
    with Session(owner_engine) as db: assert db.get(CompanySubscription,UUID(org['id'])).starts_on==start
