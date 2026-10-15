"""Opt-in real Stripe TEST acceptance; only disposable DB/company/customer/test clock."""
import os
import time
from uuid import UUID, uuid4
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from conftest import owner_engine, login, company
from app.database import engine, context
from app.models import Organization, CompanySubscription, now
from app.company_billing import provider, service, recurring
from app.company_billing.models import CompanyBilling
from test_company_subscriptions import fleet

pytestmark=pytest.mark.skipif(os.environ.get('STRIPE_TEST_ACCEPTANCE')!='1',reason='explicit real Stripe test acceptance only')


def test_real_monthly_trial_renewals_quantity_replacement_failure_and_zero_fleet(client,monkeypatch):
    assert os.environ['STRIPE_SECRET_KEY'].startswith('sk_test_')
    # On rejection expose only documented error identifiers, never payloads/URLs/secrets.
    request=provider.httpx.request
    def checked_request(*args,**kwargs):
        response=request(*args,**kwargs)
        if response.status_code>=400 and response.status_code!=404:
            error=response.json().get('error',{})
            raise AssertionError(f"Stripe rejected test request: status={response.status_code}, type={error.get('type')}, code={error.get('code')}, param={error.get('param')}")
        return response
    monkeypatch.setattr(provider.httpx,'request',checked_request)
    login(client);org=company(client,'billing-acceptance')
    identity=UUID(org['id']);frozen=int(time.time());cutoff=frozen+172800
    clock=provider.call('POST','test_helpers/test_clocks',{'frozen_time':frozen,'name':'Dispatra disposable recurring acceptance'},str(uuid4()))
    customer_id=None
    def advance(target):
        provider.call('POST','test_helpers/test_clocks/'+clock['id']+'/advance',{'frozen_time':target},str(uuid4()))
        for _ in range(90):
            if provider.call('GET','test_helpers/test_clocks/'+clock['id'])['status']=='ready': return
            time.sleep(1)
        raise AssertionError('Stripe test clock did not finish advancing.')
    def card(token):
        method=provider.call('POST','payment_methods',{'type':'card','card[token]':token},str(uuid4()))
        provider.call('POST','payment_methods/'+method['id']+'/attach',{'customer':customer_id},str(uuid4()))
        provider.call('POST','customers/'+customer_id,{'invoice_settings[default_payment_method]':method['id']},str(uuid4()))
        with Session(engine) as db,db.begin():
            context(db,platform=True)
            row=db.get(CompanyBilling,identity)
            service.save_card(db,db.get(Organization,identity),row,provider.call('GET','payment_methods/'+method['id']))
            recurring.request_sync(row)
        return method['id']
    def remote_sub():
        with Session(owner_engine) as db: sub_id=db.get(CompanyBilling,identity).stripe_subscription
        return provider.call('GET','subscriptions/'+sub_id)
    def finish_invoice(sub, expected, pay=True):
        invoice=provider.call('GET','invoices/'+sub['latest_invoice'])
        assert invoice['amount_due']==expected
        if invoice['status']=='draft':
            invoice=provider.call('POST','invoices/'+invoice['id']+'/finalize',{'auto_advance':'false'},str(uuid4()))
        if pay and invoice['status']=='open':
            invoice=provider.call('POST','invoices/'+invoice['id']+'/pay',{},str(uuid4()))
        return invoice
    try:
        customer=provider.call('POST','customers',{'test_clock':clock['id'],'metadata[organization_id]':str(identity),'name':'Disposable Dispatra billing acceptance'},str(uuid4()))
        customer_id=customer['id']
        with Session(owner_engine) as db,db.begin():
            saved=db.get(Organization,identity)
            saved.trial_started_at=datetime.fromtimestamp(frozen,timezone.utc)-timedelta(days=28)
            saved.trial_ends_at=datetime.fromtimestamp(cutoff,timezone.utc)
            terms=db.get(CompanySubscription,identity);terms.monthly_price=Decimal('12.34')
            db.add(CompanyBilling(organization_id=identity,stripe_customer=customer_id,consented_at=now(),setup_terms_version=terms.version,consent_terms={'acceptance':'disposable test only'}))
            saved.billing_enrolled=True
        good=card('tok_visa')
        intent=provider.call('POST','setup_intents',{'customer':customer_id,'payment_method':good,'payment_method_types[0]':'card','usage':'off_session','confirm':'true'},str(uuid4()))
        assert intent['status']=='succeeded'
        fleet(org,1);fleet(org,1,active=False);fleet(org,1,archived=True)
        recurring.synchronize(identity)
        first=remote_sub();assert first['status']=='trialing' and first['trial_end']==cutoff
        assert first['items']['data'][0]['quantity']==2
        assert finish_invoice(first,0)['status']=='paid'
        # End the original trial and pay the real first monthly subscription invoice.
        advance(cutoff+7200)
        first=remote_sub();invoice=finish_invoice(first,2468)
        assert invoice['status']=='paid' and invoice['amount_paid']==2468
        recurring.synchronize(identity)
        with Session(owner_engine) as db:
            assert db.get(CompanyBilling,identity).subscription_status=='active'
            assert db.get(Organization,identity).billing_access_until is not None
        before_id=first['id'];card('tok_mastercard');recurring.synchronize(identity)
        assert remote_sub()['id']==before_id
        fleet(org,1)
        with owner_engine.begin() as db: db.execute(text('UPDATE company_subscriptions SET monthly_price=15.50,version=version+1 WHERE organization_id=:id'),{'id':identity})
        recurring.synchronize(identity)
        sub=remote_sub();assert sub['id']==before_id and sub['items']['data'][0]['quantity']==3
        assert sub['latest_invoice']==invoice['id'], 'Fleet/rate edits must not create an immediate invoice.'
        # Next monthly renewal uses exact new saved rate × current non-archived fleet.
        advance(sub['items']['data'][0]['current_period_end']+7200)
        sub=remote_sub();invoice=finish_invoice(sub,4650)
        assert invoice['status']=='paid'
        recurring.synchronize(identity)
        # Deliberately fail a later renewal, retain recovery URL, then recover the same invoice.
        card('tok_chargeCustomerFail');recurring.synchronize(identity)
        sub=remote_sub();advance(sub['items']['data'][0]['current_period_end']+7200)
        sub=remote_sub();failed=finish_invoice(sub,4650,pay=False)
        if failed['status']=='open':
            # Expected card declines return HTTP402; allow the transport to map this privately.
            monkeypatch.setattr(provider.httpx,'request',request)
            try: provider.call('POST','invoices/'+failed['id']+'/pay',{},str(uuid4()))
            except Exception: pass
            monkeypatch.setattr(provider.httpx,'request',checked_request)
        recurring.synchronize(identity)
        with Session(owner_engine) as db:
            billing=db.get(CompanyBilling,identity)
            assert billing.invoice_url and db.get(Organization,identity).billing_access_until is None
        recovered=card('tok_visa');recurring.synchronize(identity)
        paid=provider.call('POST','invoices/'+failed['id']+'/pay',{'payment_method':recovered},str(uuid4()))
        assert paid['status']=='paid'
        recurring.synchronize(identity)
        # Empty fleet generates a zero-total renewal on the same subscription.
        with owner_engine.begin() as db: db.execute(text('UPDATE vehicles SET archived_at=now() WHERE organization_id=:id'),{'id':identity})
        recurring.synchronize(identity)
        sub=remote_sub();assert sub['items']['data'][0]['quantity']==0
        advance(sub['items']['data'][0]['current_period_end']+7200)
        sub=remote_sub();assert finish_invoice(sub,0)['status']=='paid'
        recurring.synchronize(identity)
        listed=provider.call('GET','subscriptions?'+__import__('urllib.parse',fromlist=['urlencode']).urlencode({'customer':customer_id,'status':'all'}))
        assert len(listed['data'])==1
    finally:
        provider.call('DELETE','test_helpers/test_clocks/'+clock['id'],key=str(uuid4()))


def test_real_late_enrollment_automatically_charges_verified_saved_card():
    assert os.environ['STRIPE_SECRET_KEY'].startswith('sk_test_')
    customer=provider.call('POST','customers',{'metadata[organization_id]':str(uuid4()),'name':'Disposable late enrollment acceptance'},str(uuid4()))
    try:
        method=provider.call('POST','payment_methods',{'type':'card','card[token]':'tok_visa'},str(uuid4()))
        provider.call('POST','payment_methods/'+method['id']+'/attach',{'customer':customer['id']},str(uuid4()))
        setup=provider.call('POST','setup_intents',{'customer':customer['id'],'payment_method':method['id'],'confirm':'true','usage':'off_session','payment_method_types[0]':'card'},str(uuid4()))
        assert setup['status']=='succeeded'
        payload={'organization':customer['metadata']['organization_id'],'customer':customer['id'],'subscription':None,'method':method['id'],
            'status':'ACTIVE','price':'1.00','currency':'CAD','quantity':2,'starts':int(time.time())-86400,'paid_until':None,'restart':False,'key':str(uuid4())}
        sub,invoice=recurring.remote(payload)
        assert sub['status']=='active' and invoice['status']=='paid' and invoice['amount_paid']==200, 'Late enrollment must attempt its first charge automatically with the verified saved card.'
    finally:
        provider.call('DELETE','customers/'+customer['id'],key=str(uuid4()))
