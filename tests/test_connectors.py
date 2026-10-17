"""Synthetic certified profiles exercise the connector engine; these tests do not certify vendor payloads."""
import json,hashlib,os
from urllib.parse import urlsplit,parse_qs
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from conftest import login,company,post,owner_engine
from app.connectors import profiles,transport,worker,imports
from app.intake import geocode
from app.integrations import client as http
from app.integrations.fields import REQUIRED
from app.connectors.catalog import BY_ID

BASE='/api/v1/companies/acme/integrations/connectors'
MAP={'id':'orderId','scheduled_at':'pickupDate','service':'serviceLevel','total':'charges.total','tax':'charges.tax',
 'shipper.id':'customer.id','shipper.name':'customer.name','shipper.email':'customer.email','stops':'stops','stop.id':'ref',
 'stop.type':'type','stop.pickup_value':'PU','stop.delivery_value':'DEL','stop.address':'street','stop.city':'city',
 'stop.province':'prov','stop.postal_code':'zip','items':'lines','item.pickup_stop':'from','item.delivery_stop':'to',
 'item.weight':'weight','item.length':'length','item.width':'width','item.height':'height'}

def check(response,code=200):
 assert response.status_code==code,response.text
 return response.json()

def profile(provider='truckmate'):
 return {'provider':provider,'revision':'synthetic-1','api_version':'test-only','evidence':'Synthetic acceptance fixture, not vendor certification',
  'base_url':'https://provider.example','identity':{'path':'/identity'},'account_id_path':'id','account_label_path':'name','identity_scope':'mapped',
  'resources':{'order':{'path':'/orders','records_path':'data','next_path':'next_cursor'}},'maps':{'order':dict(MAP)},
  'weight_unit':'lb','dimension_unit':'in','currency':'CAD','authorize_url':'https://login.example/authorize',
  'token_url':'https://login.example/token','client_id_env':'TEST_CONNECTOR_ID','client_secret_env':'TEST_CONNECTOR_SECRET','session_header':'X-Session-Token'}

def shipment(identity='REMOTE-1',missing=False):
 def stop(ref,kind):return {'ref':ref,'type':kind,'street':'1 Test Street','city':'Vancouver','prov':'BC','zip':'V6B 1A1'}
 return {'orderId':identity,'pickupDate':'2026-10-15T09:00:00','serviceLevel':'same_day','charges':{'total':'210.00','tax':'10.00'},
  'customer':{'id':'CUSTOMER-1','name':'Test Shipper','email':'shipper@test.example'},'stops':[stop('P1','PU'),stop('D1','DEL')],
  'lines':[] if missing else [{'from':'P1','to':'D1','weight':10,'length':12,'width':10,'height':8}]}

@pytest.fixture
def configured(client,monkeypatch,tmp_path):
 monkeypatch.setenv('MAILBOX_ENCRYPTION_KEY',Fernet.generate_key().decode());monkeypatch.setenv('TEST_CONNECTOR_ID','client-id');monkeypatch.setenv('TEST_CONNECTOR_SECRET','client-secret')
 monkeypatch.setenv('CONNECTOR_PUBLIC_ORIGIN','https://dispatra.example')
 path=tmp_path/'profiles.json';path.write_text(json.dumps({p:profile(p) for p in BY_ID}));monkeypatch.setenv('TMS_CONNECTOR_PROFILES_FILE',str(path))
 monkeypatch.setattr(geocode,'verify',lambda street,postal:{'text':street,'latitude':49.28,'longitude':-123.11})
 calls=[];rows=[]
 def request(method,target,headers,body=None):
  calls.append((method,target,headers,body))
  if '/identity' in target:return {'id':'account-1','name':'Test Transport'}
  return {'data':rows.copy()}
 monkeypatch.setattr(http,'request',request)
 monkeypatch.setattr(transport,'token',lambda *args,**kwargs:{'access_token':'test-token','refresh_token':'test-refresh','expires_in':3600})
 login(client);company(client);login(client,'dispatch','acme','dispatcher')
 return {'path':path,'rows':rows,'calls':calls}

def connection():
 with owner_engine.connect() as db:return db.execute(text("SELECT organization_id,id FROM connector_connections WHERE provider='truckmate'")).one()

def connect_truckmate(client,key=None):
 return check(post(client,BASE+'/truckmate/connect',{'credentials':{'base_url':'https://provider.example','api_key':'private-test-key'}},key))

def test_unavailable_catalogue_is_visible_and_connections_remain_gated(client,configured,monkeypatch):
 monkeypatch.delenv('TMS_CONNECTOR_PROFILES_FILE')
 rows=check(client.get(BASE));assert [r['id'] for r in rows]==['rose-rocket','cartoncloud','truckbase','tailwind','ascendtms']
 assert all(not r['ready'] and r['status']=='ACTIVATION_REQUIRED' for r in rows)
 check(post(client,BASE+'/truckmate/connect',{'credentials':{'api_key':'not-stored'}}),503)
 with owner_engine.connect() as db:assert db.scalar(text('SELECT count(*) FROM connector_connections'))==0

def test_incomplete_or_wrong_provider_profile_never_activates(client,configured):
 p=profile();p['maps']['order'].pop('tax');configured['path'].write_text(json.dumps({'truckmate':p}))
 assert profiles.installed('truckmate') is None
 check(post(client,BASE+'/truckmate/connect',{'credentials':{}}),503)

def test_existing_legacy_connection_remains_visible_until_disconnected(client,configured):
 assert 'truckmate' not in [r['id'] for r in check(client.get(BASE))]
 connected=connect_truckmate(client)['connector']
 assert 'truckmate' in [r['id'] for r in check(client.get(BASE))]
 check(post(client,BASE+'/truckmate/disconnect',{'version':connected['version']}))
 assert 'truckmate' not in [r['id'] for r in check(client.get(BASE))]
 with owner_engine.connect() as db:
  assert db.scalar(text("SELECT count(*) FROM connector_connections WHERE provider='truckmate'"))==1

def test_credentials_are_encrypted_and_retries_are_payload_bound(client,configured):
 key=str(uuid4());first=connect_truckmate(client,key);assert first['connector']['status']=='CONNECTING'
 assert 'private-test-key' not in json.dumps(first)
 assert connect_truckmate(client,key)==first
 check(post(client,BASE+'/truckmate/connect',{'credentials':{'base_url':'https://provider.example','api_key':'changed'}},key),409)
 with owner_engine.connect() as db:
  stored=db.scalar(text('SELECT credentials FROM connector_connections'));assert 'private-test-key' not in stored
  assert db.scalar(text('SELECT count(*) FROM tms_connections'))==0
  assert 'private-test-key' not in str(db.execute(text("SELECT result FROM operations WHERE key LIKE '%connector%'")).all())

def test_hidden_mapping_imports_multi_stop_units_and_price_once(client,configured):
 configured['rows'].append(shipment());connect_truckmate(client);org,identity=connection()
 assert worker.sync(org,identity)
 row=next(r for r in check(client.get(BASE)) if r['id']=='truckmate');assert row['status']=='CONNECTED' and row['imported']==1
 imported=[o for o in check(client.get('/api/v1/companies/acme/orders')) if o['facts'].get('external_reference')=='REMOTE-1']
 assert len(imported)==1 and imported[0]['pricing']['total']=='210.00'
 assert imported[0]['facts']['items'][0]['weight_kg']=='4.54'
 assert imported[0]['facts']['items'][0]['length_cm']=='30.48'
 check(post(client,BASE+'/truckmate/sync',{'version':row['version']}));assert worker.sync(org,identity)
 assert len([o for o in check(client.get('/api/v1/companies/acme/orders')) if o['facts'].get('external_reference')=='REMOTE-1'])==1
 assert check(client.get('/api/v1/companies/acme/integrations/tms')) is None

def test_incomplete_order_is_held_for_review_without_invented_cargo(client,configured):
 configured['rows'].append(shipment(missing=True));connect_truckmate(client);org,identity=connection();assert worker.sync(org,identity)
 review=check(client.get(BASE+'/truckmate/review'));assert len(review)==1 and 'Item list with at least one item' in review[0]['reasons']
 assert 'payload' not in review[0] and 'maps' not in review[0]
 assert not [o for o in check(client.get('/api/v1/companies/acme/orders')) if o['facts'].get('external_reference')=='REMOTE-1']

def test_disconnect_invalidates_inflight_worker_and_preserves_imported_data(client,configured):
 connect_truckmate(client);org,identity=connection();job=worker.claim(org,identity);assert job
 row=next(r for r in check(client.get(BASE)) if r['id']=='truckmate')
 check(post(client,BASE+'/truckmate/disconnect',{'version':row['version']}))
 from sqlalchemy.orm import Session
 from app.database import engine,context
 with Session(engine) as db,db.begin():
  context(db,org)
  with pytest.raises(imports.Stale):imports.current(db,org,identity,job['lease'])
 with owner_engine.connect() as db:assert db.scalar(text('SELECT credentials FROM connector_connections')) is None

def test_oauth_state_is_pkce_bound_single_use_and_redirects_without_tokens(client,configured):
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));query=parse_qs(urlsplit(reply['authorization_url']).query)
 assert query['code_challenge_method']==['S256'] and len(query['code_challenge'][0])>30
 state=query['state'][0]
 check(client.get(BASE+'/cartoncloud/callback',params={'state':state,'code':'auth-code'}),400)
 response=client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'auth-code'},follow_redirects=False)
 assert response.status_code==303 and response.headers['location']=='https://dispatra.example/acme/integrations'
 check(client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'auth-code'}),400)
 with owner_engine.connect() as db:
  assert db.scalar(text('SELECT state_hash FROM connector_authorizations'))==hashlib.sha256(state.encode()).hexdigest()
  assert 'auth-code' not in db.scalar(text("SELECT credentials FROM connector_connections WHERE provider='rose-rocket'"))

def test_each_provider_uses_internal_complete_profile_without_public_mapping(client,configured):
 for provider, info in BY_ID.items():
  if info.auth=='request_setup':
   assert profiles.installed(provider) is None
   check(post(client,BASE+f'/{provider}/connect',{'credentials':{}}),409)
   continue
  assert profiles.installed(provider) is not None
  creds={name:'test-credential' for name,_,_ in BY_ID[provider].credential_fields}
  if 'base_url' in creds:creds['base_url']='https://provider.example'
  check(post(client,BASE+f'/{provider}/connect',{'credentials':creds}))
  assert all('maps' not in row and 'resources' not in row for row in check(client.get(BASE)))

def test_connections_remain_company_scoped_and_wrong_company_callback_is_refused(client,configured):
 connect_truckmate(client)
 login(client);company(client,'other');login(client,'dispatch','acme','dispatcher')
 check(client.get('/api/v1/companies/other/integrations/connectors'),404)
 login(client,'dispatch','other','dispatcher')
 assert all(row['version'] is None for row in check(client.get('/api/v1/companies/other/integrations/connectors')))
 with owner_engine.connect() as db:assert db.scalar(text('SELECT count(*) FROM connector_connections'))==1

def test_pagination_refuses_cross_host_and_unexpected_formats(configured,monkeypatch):
 p=profiles.Profile.model_validate(profile());resource=profiles.Resource(path='/orders',records_path='data',next_path='next',next_kind='url')
 monkeypatch.setattr(http,'request',lambda *args,**kwargs:{'data':[],'next':'https://attacker.example/steal'})
 with pytest.raises(transport.Error,match='PAGINATION_HOST'):transport.fetch('truckmate',p,resource,{'api_key':'secret'})
 monkeypatch.setattr(http,'request',lambda *args,**kwargs:{'unexpected':[]})
 with pytest.raises(transport.Error,match='FORMAT_CHANGED'):transport.fetch('truckmate',p,resource,{'api_key':'secret'})

def test_shipper_role_and_forced_rls_cannot_read_connector_secrets(client,configured):
 from conftest import customer
 from sqlalchemy.orm import Session
 from app.database import engine,context
 connect_truckmate(client);org,_=connection()
 customer(client)
 login(client,'customer','acme','customer')
 check(client.get(BASE),403)
 with Session(engine) as db,db.begin():
  context(db,org,shipper_id=uuid4())
  assert db.scalar(text('SELECT count(*) FROM connector_connections'))==0

def test_oauth_decline_never_queues_credentials_and_state_expiry_is_enforced(client,configured):
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 response=client.get(BASE+'/rose-rocket/callback',params={'state':state,'error':'access_denied'},follow_redirects=False);assert response.status_code==303
 with owner_engine.connect() as db:assert db.scalar(text('SELECT count(*) FROM connector_connections'))==0
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 with owner_engine.begin() as db:db.execute(text("UPDATE connector_authorizations SET expires_at=now()-interval '1 minute' WHERE state_hash=:value"),{'value':hashlib.sha256(state.encode()).hexdigest()})
 check(client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'test'}),400)

def test_two_connectors_with_same_remote_order_id_keep_separate_links_and_stops(client,configured):
 configured['rows'].append(shipment());connect_truckmate(client);org,identity=connection();assert worker.sync(org,identity)
 check(post(client,BASE+'/mcleod/connect',{'credentials':{'tenant':'test','api_key':'test-key'}}))
 with owner_engine.connect() as db:second=db.scalar(text("SELECT id FROM connector_connections WHERE provider='mcleod'"))
 assert worker.sync(org,second)
 rows=[o for o in check(client.get('/api/v1/companies/acme/orders')) if o['facts'].get('external_reference')=='REMOTE-1']
 assert len(rows)==2 and rows[0]['facts']['stops'][0]['id']!=rows[1]['facts']['stops'][0]['id']
 with owner_engine.connect() as db:assert db.scalar(text("SELECT count(*) FROM connector_links WHERE kind='ORDER'"))==2

def test_worker_identity_failure_does_not_claim_connected(client,configured,monkeypatch):
 connect_truckmate(client);org,identity=connection()
 monkeypatch.setattr(http,'request',lambda *args,**kwargs:{'unexpected':'no-account'})
 assert not worker.sync(org,identity)
 row=next(r for r in check(client.get(BASE)) if r['id']=='truckmate');assert row['status']=='SYNC_FAILED' and row['last_sync_at'] is None

@pytest.mark.parametrize('provider',['rose-rocket','cartoncloud'])
def test_priority_connectors_authorize_then_import_through_existing_commands(client,configured,provider,monkeypatch):
 configured['rows'].append(shipment())
 if provider=='cartoncloud':
  p=profile(provider);p['identity_scope']='cartoncloud_tenant'
  p['resources']['order']['path']='/tenants/{account_id}/orders'
  configured['path'].write_text(json.dumps({provider:p}))
  old_request=http.request
  def request(method,target,headers,body=None):
   if '/identity' in target:return {'id':'person-id','tenants':[{'id':'account-1','name':'Test Transport'}]}
   assert '/tenants/account-1/' in target
   assert headers['Accept-Version']=='8'
   return old_request(method,target,headers,body)
  monkeypatch.setattr(http,'request',request)
 reply=check(post(client,BASE+f'/{provider}/connect',{}))
 state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 response=client.get(BASE+f'/{provider}/callback',params={'state':state,'code':'fixture-code'},follow_redirects=False)
 assert response.status_code==303
 with owner_engine.connect() as db:
  org,identity=db.execute(text('SELECT organization_id,id FROM connector_connections WHERE provider=:provider'),{'provider':provider}).one()
 assert worker.sync(org,identity)
 row=next(r for r in check(client.get(BASE)) if r['id']==provider)
 assert row['status']=='CONNECTED' and row['imported']==1
 orders=check(client.get('/api/v1/companies/acme/orders'))
 assert len([o for o in orders if o['facts'].get('external_reference')=='REMOTE-1'])==1
 with owner_engine.connect() as db:
  assert db.scalar(text('SELECT account_id FROM connector_connections WHERE id=:id'),{'id':identity})=='account-1'
