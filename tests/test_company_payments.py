"""Real restricted PostgreSQL access tests; Stripe transport is an explicit test double."""
import hashlib
import hmac
import json
import time
from uuid import uuid4
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conftest import login, company, post, customer, owner_engine, HEADERS, PASSWORD
from app.database import engine, context
from app.main import app
from app.models import Organization, now
from app.company_billing import provider, recurring

BASE = '/api/v1/companies/acme/billing/payment-method'


def clear_card(identity, trial=False):
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET payment_method_valid_until=NULL, trial_started_at=now()-interval '31 days', trial_ends_at=now()+(:days * interval '1 day') WHERE id=:id"), {'id': identity, 'days': 1 if trial else -1})


@pytest.fixture
def stripe(monkeypatch):
    for key, value in {'STRIPE_SECRET_KEY':'sk_test_fake', 'STRIPE_WEBHOOK_SECRET':'whsec_test', 'BILLING_WEB_ORIGIN':'http://127.0.0.1:3000'}.items():
        monkeypatch.setenv(key, value)
    with owner_engine.begin() as db: db.execute(text("UPDATE subscription_plans SET monthly_price=10,currency='CAD'"))
    state = {'subscriptions':{}, 'invoices':{}, 'products':{}, 'calls': [], 'sessions': {}, 'customers': {}, 'intents': {}, 'methods': {}, 'charges': {}}
    def call(method, path, data=None, key=None, **options):
        state['calls'].append((method, path, data, key))
        if path == 'products/' + recurring.PRODUCT:
            return {'id':recurring.PRODUCT,'livemode':False}
        if path.startswith('subscriptions?'):
            from urllib.parse import parse_qs
            customer_id = parse_qs(path.split('?',1)[1])['customer'][0]
            return {'data':[r.copy() for r in state['subscriptions'].values() if r['customer']==customer_id], 'has_more':False}
        if path == 'subscriptions':
            assert data['collection_method']=='charge_automatically' and data['payment_behavior']=='allow_incomplete' and data['off_session']=='true'
            assert data['items[0][price_data][recurring][interval]']=='month'
            identity = 'sub_' + str(len(state['subscriptions'])+1)
            invoice_id = 'in_' + identity
            trial_end = data.get('trial_end')
            row = {'id':identity, 'customer':data['customer'], 'livemode':False,
                'metadata':{'organization_id':data['metadata[organization_id]']}, 'currency':data['items[0][price_data][currency]'],
                'status':'trialing' if trial_end else 'active', 'trial_end':trial_end,
                'default_payment_method':data.get('default_payment_method'), 'latest_invoice':invoice_id,
                'items':{'data':[{'id':'si_'+identity, 'quantity':data['items[0][quantity]'],
                    'price':{'unit_amount_decimal':data['items[0][price_data][unit_amount]']},
                    'current_period_end':trial_end or int(time.time())+2592000}]}}
            state['subscriptions'][identity]=row
            state['invoices'][invoice_id]={'id':invoice_id,'customer':data['customer'],'livemode':False,'status':'paid','hosted_invoice_url':'https://invoice.stripe.com/i/test'}
            return row
        if path.startswith('subscriptions/'):
            row=state['subscriptions'][path.split('/')[1]]
            if method=='DELETE':
                assert data=={'invoice_now':'false','prorate':'false'}
                row['status']='canceled'
            if method=='POST':
                assert data['proration_behavior']=='none'
                if 'items[0][id]' in data:
                    assert data['items[0][id]']==row['items']['data'][0]['id']
                    row['items']['data'][0]['quantity']=data['items[0][quantity]']
                    row['items']['data'][0]['price']['unit_amount_decimal']=data['items[0][price_data][unit_amount]']
                if 'default_payment_method' in data: row['default_payment_method']=data['default_payment_method']
            return row
        if path.startswith('invoices/'): return state['invoices'][path.split('/')[1]]
        if path == 'customers':
            identity = 'cus_' + data['metadata[organization_id]'].replace('-', '')
            state['customers'].setdefault(identity, {'id': identity, 'invoice_settings': {}})
            return state['customers'][identity]
        if path == 'checkout/sessions':
            assert set(data) <= {'mode', 'customer', 'payment_method_types[0]', 'success_url', 'cancel_url', 'metadata[organization_id]', 'metadata[setup_nonce]', 'setup_intent_data[metadata][organization_id]', 'setup_intent_data[metadata][setup_nonce]'}, 'Checkout accepts only its supported SetupIntent subset.'
            assert data['mode'] == 'setup' and data['payment_method_types[0]'] == 'card'
            identity = 'cs_test_' + data['metadata[setup_nonce]']
            state['sessions'].setdefault(identity, {'id': identity, 'url': 'https://checkout.stripe.com/c/pay/' + identity, 'customer': data['customer'], 'mode':'setup', 'status':'open', 'metadata':{'organization_id':data['metadata[organization_id]'], 'setup_nonce':data['metadata[setup_nonce]']}})
            return state['sessions'][identity]
        if path.startswith('charges?'):
            from urllib.parse import parse_qs
            query = parse_qs(path.split('?',1)[1])
            rows = state['charges'].get(query['customer'][0], [])
            if query.get('starting_after'):
                start = next(i+1 for i,row in enumerate(rows) if row['id'] == query['starting_after'][0])
                rows = rows[start:]
            return {'data':rows[:20], 'has_more':len(rows)>20}
        group, identity = path.split('/', 1)
        if group == 'checkout': return state['sessions'][identity.split('/', 1)[1]]
        if group == 'customers':
            row = state['customers'][identity]
            if method == 'POST': row['invoice_settings']['default_payment_method'] = data['invoice_settings[default_payment_method]']
            return row
        return state[{'setup_intents':'intents', 'payment_methods':'methods'}[group]][identity]
    monkeypatch.setattr(provider, 'call', call)
    return state


def ready(stripe, session_id, method_id='pm_card'):
    session = stripe['sessions'][session_id]
    intent_id = 'seti_' + session_id
    session.update(status='complete', setup_intent=intent_id)
    stripe['intents'][intent_id] = {'status':'succeeded', 'customer':session['customer'], 'metadata':session['metadata'], 'payment_method':method_id}
    stripe['methods'][method_id] = {'id':method_id, 'type':'card', 'customer':session['customer'], 'card':{'brand':'visa', 'last4':'4242', 'exp_month':12, 'exp_year':now().year + 3}}


def start(client, path=BASE, key=None):
    response = post(client, path + '/setup', {'consent':True,'terms_version':client.get(path).json()['terms_version']}, key)
    assert response.status_code == 200, response.text
    return response.json()['url'].rsplit('/', 1)[1]


def notification(client, event, timestamp=None, secret='whsec_test'):
    raw = json.dumps(event).encode()
    stamp = str(timestamp or int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    # Signature auth works without browser Origin/X-Requested-With.
    with TestClient(app) as sender:
        return sender.post('/api/v1/billing/stripe/webhook', content=raw, headers={'Stripe-Signature':f't={stamp},v1={signature}'})


def test_no_card_trial_cutoff_and_admin_activation_cannot_bypass(client, stripe):
    login(client); org = company(client); clear_card(org['id'], trial=True)
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(BASE).json()['required'] is False
    assert client.get('/api/v1/companies/acme/settings').status_code == 200
    api_key = post(client, '/api/v1/companies/acme/integrations/api-key', {}).json()['key']
    clear_card(org['id'])
    account = client.get('/api/v1/auth/me').json()
    assert account['organization']['payment_required'] is True
    for path in ('settings', 'catalog', 'activity', 'orders'):
        assert client.get('/api/v1/companies/acme/' + path).status_code == 403
    with TestClient(app, headers={'Authorization': 'Bearer ' + api_key}) as key_client:
        assert key_client.get('/api/v1/companies/acme/catalog').status_code == 401
    assert client.get(BASE).status_code == 200
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(BASE).json()['required'] is True
    with Session(owner_engine) as db:
        assert not db.get(Organization, org['id']).active
        assert not db.scalar(select(Organization.id).where(Organization.active.is_(True)))
    login(client)
    detail = client.get(f"/api/v1/platform/organizations/{org['id']}").json()
    assert detail['workspace_enabled'] and detail['payment_required'] and not detail['active']
    suspend = post(client, f"/api/v1/platform/organizations/{org['id']}/suspend", {'version':detail['version']}).json()
    activate = post(client, f"/api/v1/platform/organizations/{org['id']}/activate", {'version':suspend['version']}).json()
    assert activate['workspace_enabled'] and not activate['active']


def test_setup_retry_and_verified_card_unlock_only_own_company(client, stripe):
    login(client); org = company(client); other = company(client, 'other'); clear_card(org['id'])
    login(client, 'dispatch', 'acme', 'dispatcher')
    operation = str(uuid4())
    session_id = start(client, key=operation)
    assert start(client, key=operation) == session_id
    assert post(client, BASE + '/confirm', {'session_id': session_id}).status_code == 409
    ready(stripe, session_id)
    saved = post(client, BASE + '/confirm', {'session_id':session_id})
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'SAVED' and saved.json()['last4'] == '4242' and saved.json()['required']
    assert not any(secret in saved.text for secret in ('cus_', 'pm_', 'seti_', 'sk_test', 'cs_test'))
    assert post(client, BASE + '/confirm', {'session_id':session_id}).json() == saved.json()
    assert recurring.synchronize(org['id'])
    assert not client.get(BASE).json()['required']
    assert client.get('/api/v1/companies/acme/settings').status_code == 200
    assert client.get('/api/v1/companies/other/billing/payment-method').status_code == 404
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM company_billing WHERE organization_id=:id'), {'id':other['id']}) == 0
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='subscription.company_payment_saved'")) == 1
    assert len(stripe['subscriptions']) == 1
    assert stripe['subscriptions']['sub_1']['items']['data'][0]['quantity'] == 0


def test_tampered_foreign_session_and_method_do_not_grant_access(client, stripe):
    login(client); org = company(client); clear_card(org['id']); login(client, 'dispatch', 'acme', 'dispatcher')
    session_id = start(client); ready(stripe, session_id)
    assert post(client, BASE + '/confirm', {'session_id':'cs_test_foreign'}).status_code == 409
    stripe['methods']['pm_card']['customer'] = 'cus_other'
    assert post(client, BASE + '/confirm', {'session_id':session_id}).status_code == 409
    assert client.get('/api/v1/auth/me').json()['organization']['payment_required']
    assert client.get('/api/v1/companies/acme/settings').status_code == 403
    assert post(client, BASE + '/setup', {'consent':False}).status_code == 422
    assert post(client, BASE + '/setup', {'consent':True, 'card_number':'4242424242424242'}).status_code == 422


def test_signed_webhook_replay_out_of_order_removal_and_expiry(client, stripe):
    login(client); org = company(client); clear_card(org['id']); login(client, 'dispatch', 'acme', 'dispatcher')
    first = start(client); ready(stripe, first)
    event = {'type':'checkout.session.completed', 'data':{'object':stripe['sessions'][first]}}
    assert notification(client, event, secret='wrong').status_code == 400
    assert notification(client, event, timestamp=int(time.time())-600).status_code == 400
    assert notification(client, event).status_code == 204
    assert client.get(BASE).json()['status'] == 'SAVED'
    second = start(client); ready(stripe, second, 'pm_replacement')
    event2 = {'type':'checkout.session.completed','data':{'object':stripe['sessions'][second]}}
    assert notification(client,event2).status_code == 204
    assert notification(client,event).status_code == 204
    customer_id = stripe['sessions'][first]['customer']
    assert stripe['customers'][customer_id]['invoice_settings']['default_payment_method'] == 'pm_replacement'
    stripe['methods']['pm_replacement']['card']['exp_year'] = now().year - 1
    assert notification(client, {'type':'payment_method.updated', 'data':{'object':{'id':'pm_replacement','customer':customer_id}}}).status_code == 204
    assert client.get(BASE).json()['status'] == 'EXPIRED'
    assert client.get('/api/v1/companies/acme/settings').status_code == 403
    stripe['customers'][customer_id]['invoice_settings']['default_payment_method'] = None
    assert notification(client, {'type':'payment_method.detached','data':{'object':{'id':'pm_replacement','customer':None}}}).status_code == 204
    assert client.get(BASE).json()['status'] == 'MISSING'
    login(client)
    history = client.get(f"/api/v1/platform/organizations/{org['id']}/audit").json()
    assert any(row['action'] == 'subscription.company_payment_removed' and row['actor_role'] == 'SYSTEM' for row in history)


def test_roles_rls_manual_suspension_and_unconfigured_provider(client, stripe, monkeypatch):
    login(client); org = company(client); other = company(client, 'other')
    owner_path = f"/api/v1/platform/organizations/{org['id']}/payment-method"
    assert client.get(owner_path).status_code == 200
    start(client, owner_path)
    login(client, 'dispatch', 'acme', 'dispatcher'); person = customer(client)
    assert client.get(owner_path).status_code == 403
    with Session(engine) as db, db.begin():
        context(db, other['id']); assert db.scalar(text('SELECT count(*) FROM company_billing')) == 0
        context(db, org['id'], shipper_id=person['id']); assert db.scalar(text('SELECT count(*) FROM company_billing')) == 0
        context(db, org['id'], driver_id=uuid4()); assert db.scalar(text('SELECT count(*) FROM company_billing')) == 0
    login(client, 'customer', 'acme', 'customer')
    assert client.get(BASE).status_code == 403
    assert client.get('/api/v1/companies/acme/billing/payments').status_code == 403
    login(client, 'dispatch', 'acme', 'dispatcher')
    monkeypatch.delenv('STRIPE_SECRET_KEY')
    assert not client.get(BASE).json()['setup_available']
    assert post(client, BASE + '/setup', {'consent':True,'terms_version':1}).status_code == 503
    with owner_engine.begin() as db: db.execute(text('UPDATE organizations SET active=false WHERE id=:id'), {'id':org['id']})
    assert client.get(BASE).status_code == 403
    assert client.post('/api/v1/auth/login',json={'portal':'dispatch','login_id':'dispatcher','password':PASSWORD}).status_code == 403


def test_expiry_without_worker_is_same_in_python_and_sql(client, stripe):
    login(client); org = company(client); clear_card(org['id'])
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET payment_method_valid_until=now()-interval '1 second',trial_activated_at=now() WHERE id=:id"), {'id':org['id']})
    with Session(owner_engine) as db:
        assert not db.get(Organization, org['id']).active
        assert db.scalar(select(Organization.id).where(Organization.active.is_(True))) is None
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(BASE).json()['required']


def test_no_trial_company_retains_workspace_access_without_card_and_creation_replay_preserves_history(client, stripe):
    login(client)
    operation = str(uuid4())
    body = {'name':'New company', 'slug':'new-company', 'admin_login':'new-desk', 'password':PASSWORD}
    response = post(client, '/api/v1/platform/organizations', body, operation)
    assert response.status_code == 201
    assert not response.json()['payment_required'] and response.json()['active']
    assert post(client, '/api/v1/platform/organizations', body, operation).json() == response.json()
    login(client, 'dispatch', 'new-company', 'new-desk')
    method = client.get('/api/v1/companies/new-company/billing/payment-method').json()
    assert not method['required'] and method['status'] == 'MISSING'
    assert client.get('/api/v1/auth/me').json()['organization']['active']
    assert client.get('/api/v1/companies/new-company/settings').status_code == 200
    assert client.get('/api/v1/companies/new-company/catalog').status_code == 200
    with Session(owner_engine) as db:
        org = db.get(Organization, response.json()['id'])
        assert org.trial_ends_at is None and org.payment_method_valid_until is None
        assert not org.payment_required and org.active
        assert db.scalar(select(Organization.id).where(Organization.active.is_(True))) == org.id
    with owner_engine.begin() as db:
        db.execute(text('UPDATE organizations SET active=false WHERE id=:id'), {'id':response.json()['id']})
    assert client.get('/api/v1/companies/new-company/settings').status_code == 403
    assert client.get('/api/v1/companies/new-company/billing/payment-method').status_code == 403


def test_payment_history_is_customer_scoped_paginated_and_keeps_exact_money(client, stripe):
    login(client); org = company(client); clear_card(org['id']); login(client, 'dispatch', 'acme', 'dispatcher')
    path = '/api/v1/companies/acme/billing/payments'
    assert client.get(path).json() == {'available':True,'items':[],'next_cursor':None}
    session = start(client)
    customer_id = stripe['sessions'][session]['customer']
    stripe['charges'][customer_id] = [dict(id=f'ch_{i}',customer=customer_id,created=1700000000+i,amount=4550,currency='cad',status='succeeded',amount_refunded=500 if i==0 else 0,receipt_url='https://pay.stripe.com/receipts/test') for i in range(21)]
    first = client.get(path)
    assert first.status_code == 200, first.text
    data = first.json()
    assert len(data['items']) == 20 and data['next_cursor'] == 'ch_19'
    assert data['items'][0]['amount']=='45.5' and data['items'][0]['refunded_amount']=='5'
    assert data['items'][0]['status']=='PARTIALLY_REFUNDED'
    assert 'customer' not in first.text and 'payment_method' not in first.text
    second = client.get(path,params={'after':data['next_cursor']}).json()
    assert len(second['items'])==1 and second['next_cursor'] is None
    assert client.get(path,params={'after':'invalid'}).status_code==422
    assert client.get('/api/v1/companies/other/billing/payments').status_code in (403,404)
    login(client)
    assert len(client.get(f"/api/v1/platform/organizations/{org['id']}/payments").json()['items'])==20


def test_payment_history_rejects_foreign_rows_filters_receipts_and_handles_charge_units(client, stripe, monkeypatch):
    from app.company_billing.history import amount
    from decimal import Decimal
    assert amount(1234,'CAD')==Decimal('12.34')
    assert amount(1234,'JPY')==Decimal('1234')
    assert amount(500,'ISK')==Decimal('5') and amount(500,'UGX')==Decimal('5')
    login(client); company(client); login(client,'dispatch','acme','dispatcher')
    session=start(client); customer_id=stripe['sessions'][session]['customer']
    charge=dict(id='ch_test',customer=customer_id,created=1700000000,amount=100,currency='cad',status='failed',receipt_url='https://evil.example/receipt')
    stripe['charges'][customer_id]=[charge]
    path='/api/v1/companies/acme/billing/payments'
    result=client.get(path).json()['items'][0]
    assert result['status']=='FAILED' and result['receipt_url'] is None
    charge['customer']='cus_other'
    assert client.get(path).status_code==503
    monkeypatch.delenv('STRIPE_SECRET_KEY')
    assert client.get(path).json()=={'available':False,'items':[],'next_cursor':None}
    assert all(method=='GET' for method,path,*_ in stripe['calls'] if path.startswith('charges?'))
