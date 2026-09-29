from datetime import timedelta
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.main import app
from app.database import engine, context
from app.models import Organization, now
from app.payments.models import StripeConnection, StripeCustomer
from app.payments import provider
from conftest import HEADERS, login, post, owner_engine
from test_manual_operations import setup, check, BASE


def configure(monkeypatch):
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_test_example')
    monkeypatch.setenv('PUBLIC_WEB_URL', 'http://localhost:3000')
    with Session(owner_engine) as db, db.begin():
        context(db, platform=True)
        organization = db.scalar(select(Organization).where(Organization.slug == 'acme'))
        db.add(StripeConnection(organization_id=organization.id, account_id='acct_company', livemode=False))


def portal_login(portal, w):
    login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])


def test_card_setup_is_scoped_idempotent_and_never_collects_card_data(setup, monkeypatch):
    w = setup
    configure(monkeypatch)
    calls = []
    def stripe(method, path, *, account=None, data=None, key=None):
        # No checked-out database connection or transaction spans a provider call.
        assert engine.pool.checkedout() == 0
        assert account == 'acct_company'
        calls.append((method,path,data,key))
        if path == 'customers': return {'id': 'cus_shipper'}
        if path == 'checkout/sessions':
            assert data['mode'] == 'setup'
            assert data['customer'] == 'cus_shipper'
            assert data['setup_intent_data[usage]'] == 'on_session'
            assert data['success_url'] == 'http://localhost:3000/acme/shipper-portal/payment-methods?card_setup=returned'
            assert not any('amount' in field or 'card[number]' in field for field in data)
            return {'id':'cs_setup', 'url':'https://checkout.stripe.com/c/pay/cs_setup'}
        if path == 'customers/cus_shipper/payment_methods':
            return {'has_more':False, 'data':[{'id':'pm_card','customer':'cus_shipper','type':'card','card':{'brand':'visa','last4':'4242','exp_month':12,'exp_year':2030,'fingerprint':'private'}}]}
        raise AssertionError(path)
    monkeypatch.setattr(provider, 'request', stripe)
    with TestClient(app,headers=HEADERS) as portal:
        portal_login(portal,w)
        initial=check(portal.get(BASE+'/shipper/payment-methods'))
        assert initial == {'configured':True,'test_mode':True,'terms':'NET30','cards':[]}
        assert post(portal,BASE+'/shipper/payment-methods/setup',{'consent':False}).status_code == 422
        assert post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True,'customer_id':'cus_foreign'}).status_code == 422
        key=str(uuid4())
        first=check(post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True},key))
        assert check(post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True},key)) == first
        assert len([call for call in calls if call[1]=='customers']) == 1
        sessions=[call for call in calls if call[1]=='checkout/sessions']
        assert sessions[0][3] == sessions[1][3]
        cards=check(portal.get(BASE+'/shipper/payment-methods'))['cards']
        assert cards == [{'id':'pm_card','brand':'visa','last4':'4242','exp_month':12,'exp_year':2030}]
        assert portal.get('/api/v1/companies/other/shipper/payment-methods').status_code == 404
    assert w['client'].get(BASE+'/shipper/payment-methods').status_code == 403
    assert w['mobile'].get(BASE+'/shipper/payment-methods').status_code == 403
    with Session(owner_engine) as db:
        rows=db.scalars(select(StripeCustomer)).all()
        assert len(rows)==1 and rows[0].shipper_id == UUID(w['shipper']['id'])


def test_payment_configuration_and_failure_do_not_fabricate_saved_cards(setup,monkeypatch):
    w=setup
    monkeypatch.delenv('STRIPE_SECRET_KEY',raising=False)
    monkeypatch.setenv('PUBLIC_WEB_URL','http://localhost:3000')
    with TestClient(app,headers=HEADERS) as portal:
        portal_login(portal,w)
        assert check(portal.get(BASE+'/shipper/payment-methods'))['configured'] is False
        assert post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True}).status_code==409
        configure(monkeypatch)
        def fail(*args,**kwargs): raise HTTPException(503,'Stripe unavailable')
        monkeypatch.setattr(provider,'request',fail)
        assert post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True}).status_code==503
        assert check(portal.get(BASE+'/shipper/payment-methods'))['cards']==[]
        with Session(owner_engine) as db,db.begin():
            row=db.scalar(select(StripeCustomer))
            row.created_at=now()-timedelta(hours=24)
        # Stripe's expired idempotency window must not create a duplicate customer.
        assert post(portal,BASE+'/shipper/payment-methods/setup',{'consent':True}).status_code==409


def test_customer_references_are_isolated_by_shipper_and_stripe_mode(setup,monkeypatch):
    w=setup
    configure(monkeypatch)
    second=check(post(w['client'],BASE+'/shippers',{**w['shipper_body'],'email':'second@example.com','name':'Second Shipper'}),201)
    with Session(owner_engine) as db,db.begin():
        org=db.scalar(select(Organization).where(Organization.slug=='acme'))
        org_id=org.id
        db.add(StripeCustomer(organization_id=org_id,shipper_id=UUID(w['shipper']['id']),account_id='acct_company',livemode=False,customer_id='cus_first'))
    with TestClient(app,headers=HEADERS) as portal:
        login(portal,'customer','acme',second['email'],second['initial_password'])
        assert check(portal.get(BASE+'/shipper/payment-methods'))['cards']==[]
    with Session(engine) as db,db.begin():
        context(db,org_id,shipper_id=second['id'])
        assert db.scalars(select(StripeCustomer)).all()==[]
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_live_example')
    with TestClient(app,headers=HEADERS) as portal:
        portal_login(portal,w)
        assert check(portal.get(BASE+'/shipper/payment-methods')) == {'configured':False,'test_mode':False,'terms':'NET30','cards':[]}


def test_stripe_transport_uses_connected_account_and_hides_provider_errors(monkeypatch):
    import json
    from urllib.error import HTTPError
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_test_example')
    def opening(request,timeout):
        assert request.full_url == 'https://api.stripe.com/v1/checkout/sessions'
        assert request.get_header('Stripe-account') == 'acct_company'
        assert request.get_header('Idempotency-key') == 'setup-key'
        assert request.get_header('Stripe-version') == provider.API_VERSION
        assert request.data == b'mode=setup&customer=cus_shipper'
        assert timeout == 20
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,limit): return json.dumps({'id':'cs_session'}).encode()
        return Response()
    monkeypatch.setattr(provider,'urlopen',opening)
    assert provider.request('POST','checkout/sessions',account='acct_company',key='setup-key',data={'mode':'setup','customer':'cus_shipper'}) == {'id':'cs_session'}
    def failing(*args,**kwargs): raise HTTPError('https://api.stripe.com',401,'private provider detail',{},None)
    monkeypatch.setattr(provider,'urlopen',failing)
    with pytest.raises(HTTPException) as error: provider.request('GET','customers/cus_shipper',account='acct_company')
    assert error.value.status_code == 503
    assert 'private provider detail' not in error.value.detail
