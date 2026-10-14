from uuid import uuid4
from sqlalchemy import text
from conftest import PASSWORD, company, confirm_trial, customer, login, owner_engine, post


def test_owner_tms_choice_persists_and_is_scoped_to_dispatcher(client):
    login(client)
    body = dict(name='TMS Company', slug='tms-company', admin_login='tms@example.com', password=PASSWORD, tms_workspace=True)
    key = str(uuid4())
    first = post(client, '/api/v1/platform/organizations', body, key)
    assert first.status_code == 201, first.text
    assert post(client, '/api/v1/platform/organizations', body, key).json() == first.json()
    assert post(client, '/api/v1/platform/organizations', {**body, 'tms_workspace': False}, key).status_code == 409
    ordinary = company(client)
    account = login(client, 'dispatch', login_id='tms@example.com')
    assert account['tms_workspace'] is True
    assert client.get('/api/v1/auth/me').json()['tms_workspace'] is True
    settings_path = '/api/v1/companies/tms-company/settings'
    saved = client.get(settings_path).json()
    assert saved['data']['tms_workspace'] is True
    # Older settings clients do not erase the onboarding preference.
    saved['data'].pop('tms_workspace')
    response = client.put(settings_path, json={'version': saved['version'], 'data': saved['data']}, headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    assert response.json()['data']['tms_workspace'] is True
    assert client.get('/api/v1/companies/acme/settings').status_code == 404
    customer(client, slug='tms-company')
    shipper = login(client, 'customer', 'tms-company', 'customer')
    assert shipper['tms_workspace'] is False
    assert client.get(settings_path).status_code == 403
    normal = login(client, 'dispatch', 'acme', 'dispatcher')
    assert normal['organization']['id'] == ordinary['id']
    assert normal['tms_workspace'] is False
    with owner_engine.begin() as db:
        db.execute(text("UPDATE company_settings SET data=data-'tms_workspace' WHERE organization_id=:id"), {'id': ordinary['id']})
    assert client.get('/api/v1/auth/me').json()['tms_workspace'] is False


def test_trial_tms_choice_survives_verification_and_payload_bound_retries(client, verification_mail):
    body = dict(slug='trial-tms', name='Trial TMS', full_name='Trial Dispatcher', email='trial-tms@example.com', password=PASSWORD, tms_workspace=True)
    key = str(uuid4())
    response = post(client, '/api/v1/auth/signup', body, key)
    assert response.status_code == 201 and response.json()['verification_required'], response.text
    assert client.get('/api/v1/auth/me').status_code == 401
    assert post(client, '/api/v1/auth/signup', {**body, 'tms_workspace': False}, key).status_code == 409
    confirmed = confirm_trial(client, body['email'])
    assert confirmed.status_code == 200, confirmed.text
    account = confirmed.json()
    assert account['tms_workspace'] is True and account['role'] == 'DISPATCHER'
    assert account['organization']['trial_started_at'] and account['organization']['trial_ends_at']
    assert post(client, '/api/v1/auth/signup', body, key).json()['tms_workspace'] is True
    assert client.get('/api/v1/companies/trial-tms/settings').json()['data']['tms_workspace'] is True
    assert login(client, 'dispatch', login_id=body['email'])['tms_workspace'] is True


def test_tms_preference_accepts_only_boolean_inputs(client, verification_mail):
    for value in ['true', 1, None]:
        body = dict(slug='invalid-tms', name='Invalid', full_name='Dispatcher', email='invalid@example.com', password=PASSWORD, tms_workspace=value)
        assert post(client, '/api/v1/auth/signup', body).status_code == 422
    login(client)
    assert post(client, '/api/v1/platform/organizations', dict(slug='invalid-tms', name='Invalid', admin_login='invalid@example.com', password=PASSWORD, tms_workspace='true')).status_code == 422


BASE = '/api/v1/companies/acme'
def set_mode(client, external, key=None, saved=None):
    saved = saved or client.get(BASE+'/settings').json()
    return client.put(BASE+'/settings', json={'version':saved['version'],'data':{**saved['data'],'tms_workspace':external}},
        headers={'Idempotency-Key':key or str(uuid4())})


def test_mode_changes_switch_default_atomically_restore_custom_rates_and_replay(client):
    login(client);company(client);login(client,'dispatch','acme','dispatcher')
    rates=client.get(BASE+'/rate-cards').json()
    chosen=next(row for row in rates if row['data']['method']=='HOURLY')
    result=client.put(BASE+'/rate-cards/'+chosen['id'],json={'version':chosen['version'],'is_default':True,'active':True,'data':chosen['data']},headers={'Idempotency-Key':str(uuid4())})
    assert result.status_code==200,result.text
    native=result.json()
    saved=client.get(BASE+'/settings').json();saved['data']['dispatch_mode']='AUTO'
    key=str(uuid4());first=set_mode(client,True,key,saved)
    assert first.status_code==200,first.text
    assert first.json()['data']['dispatch_mode']=='AUTO'
    external=client.get(BASE+'/rate-cards').json()
    default=next(row for row in external if row['is_default'])
    assert default['data']['name']==next(row['data']['name'] for row in rates if row['code']=='IMPORTED') and default['data']['method']=='IMPORTED'
    assert len([row for row in external if row['is_default']])==1
    assert set_mode(client,True,key,saved).json()==first.json()
    assert client.get(BASE+'/rate-cards').json()==external
    assert set_mode(client,False,key,saved).status_code==409
    restored=set_mode(client,False)
    assert restored.status_code==200,restored.text
    assert restored.json()['data']['dispatch_mode']=='AUTO'
    default=next(row for row in client.get(BASE+'/rate-cards').json() if row['is_default'])
    assert default['id']==native['id'] and default['data']==native['data']
    assert client.get('/api/v1/auth/me').json()['tms_workspace'] is False


def test_external_provisioning_selects_imported_tms_price_before_first_login(client,verification_mail):
    login(client)
    created=post(client,'/api/v1/platform/organizations',dict(slug='external',name='External',admin_login='external@example.com',password=PASSWORD,tms_workspace=True))
    assert created.status_code==201,created.text
    login(client,'dispatch',login_id='external@example.com')
    default=next(row for row in client.get('/api/v1/companies/external/rate-cards').json() if row['is_default'])
    assert default['data']['method']=='IMPORTED' and default['data']['name']=='Imported price'
    post(client,'/api/v1/auth/logout',{})
    signup=post(client,'/api/v1/auth/signup',dict(slug='trial-external',name='Trial External',full_name='Dispatcher',email='external-trial@example.com',password=PASSWORD,tms_workspace=True))
    assert signup.status_code==201,signup.text
    assert confirm_trial(client,'external-trial@example.com').status_code==200
    default=next(row for row in client.get('/api/v1/companies/trial-external/rate-cards').json() if row['is_default'])
    assert default['data']['method']=='IMPORTED'


def test_mode_rejection_leaves_rates_settings_and_other_company_unchanged(client):
    login(client);company(client);company(client,'other');login(client,'dispatch','acme','dispatcher')
    saved=client.get(BASE+'/settings').json();rates=client.get(BASE+'/rate-cards').json()
    stale={**saved,'version':saved['version']+1}
    assert set_mode(client,True,saved=stale).status_code==409
    assert client.get(BASE+'/settings').json()==saved and client.get(BASE+'/rate-cards').json()==rates
    assert client.put('/api/v1/companies/other/settings',json={'version':saved['version'],'data':{**saved['data'],'tms_workspace':True}},headers={'Idempotency-Key':str(uuid4())}).status_code==404
    login(client,'dispatch','other','dispatcher')
    other=next(row for row in client.get('/api/v1/companies/other/rate-cards').json() if row['is_default'])
    assert other['data']['method']!='IMPORTED'


from test_manual_operations import setup, new_order

def test_mode_changes_preserve_existing_order_pricing_and_imported_tax(setup):
    w=setup;client=w['client'];order=new_order(w);before=client.get(BASE+'/orders/'+order['id']).json()
    assert set_mode(client,True).status_code==200
    assert client.get(BASE+'/orders/'+order['id']).json()==before
    from app.operations.schemas import Booking
    from app.operations.pricing import calculate
    from test_manual_operations import booking
    from decimal import Decimal
    default=next(row for row in client.get(BASE+'/rate-cards').json() if row['is_default'])
    data=Booking.model_validate({**booking(w['shipper']['id'],w['service'],None,default['id']),
        'imported_total':'105.23','imported_tax':'5.11','external_reference':'TMS-123'})
    price=calculate(data,{'card_id':default['id'],'card_version':default['version'],'card':default['data'],
        'settings':client.get(BASE+'/settings').json()['data']})
    assert price['total']=='105.23' and price['tax']=='5.11' and price['subtotal']=='100.12'
    assert set_mode(client,False).status_code==200
    assert client.get(BASE+'/orders/'+order['id']).json()==before
