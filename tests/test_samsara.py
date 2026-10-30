import json
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from conftest import login, company, post, owner_engine
from app.connectors import samsara, worker
from app.integrations import client as transport

BASE='/api/v1/companies/acme/integrations/connectors'
TOKEN='private-samsara-test-token'

@pytest.fixture
def samsara_account(client,monkeypatch):
    monkeypatch.setenv('MAILBOX_ENCRYPTION_KEY',Fernet.generate_key().decode())
    calls=[]
    def request(method,url,headers,body=None):
        assert method=='GET' and url=='https://api.samsara.com/me'
        assert headers['Authorization']=='Bearer '+TOKEN
        calls.append(url)
        return {'data':{'id':'samsara-org-1','name':'Test Fleet'}}
    monkeypatch.setattr(transport,'request',request)
    login(client);company(client);login(client,'dispatch','acme','dispatcher')
    return calls

def body(version=None):return {'version':version,'credentials':{'api_key':TOKEN}}
def connect(client):
    response=post(client,BASE+'/samsara/connect',body())
    assert response.status_code==200,response.text
    return response.json()['connector']

def test_token_test_does_not_save_and_connect_reverifies_encrypted_token(client,samsara_account):
    row=next(r for r in client.get(BASE).json() if r['id']=='samsara')
    assert row['ready'] and row['auth']=='bearer' and len(row['credential_fields'])==1
    response=client.post(BASE+'/samsara/test',json=body())
    assert response.status_code==200 and response.json()['account']=='Test Fleet'
    with owner_engine.connect() as db:assert db.scalar(text("SELECT count(*) FROM connector_connections WHERE provider='samsara'"))==0
    key=str(uuid4());response=post(client,BASE+'/samsara/connect',body(),key)
    assert response.status_code==200,response.text
    row=response.json()['connector'];assert row['status']=='CONNECTED' and row['account']=='Test Fleet'
    assert len(samsara_account)==2
    assert post(client,BASE+'/samsara/connect',body(),key).json()==response.json()
    assert len(samsara_account)==2
    assert TOKEN not in json.dumps(response.json())
    with owner_engine.connect() as db:
        saved=db.execute(text("SELECT credentials,next_attempt_at,organization_id,id FROM connector_connections WHERE provider='samsara'")).one()
        assert TOKEN not in saved.credentials and saved.next_attempt_at is None
    assert worker.claim(saved.organization_id,saved.id) is None
    assert post(client,BASE+'/samsara/connect',body()).status_code==409
    checked=post(client,BASE+'/samsara/sync',{'version':row['version']})
    assert checked.status_code==200 and checked.json()['status']=='CONNECTED'
    row=checked.json()
    assert post(client,BASE+'/samsara/disconnect',{'version':row['version']}).json()['status']=='DISCONNECTED'
    with owner_engine.connect() as db:assert db.scalar(text("SELECT credentials FROM connector_connections WHERE provider='samsara'")) is None

@pytest.mark.parametrize('code',['UNAUTHORIZED','FORBIDDEN','RATE_LIMITED','CONNECTION_FAILED','REDIRECT_REFUSED','INVALID_JSON'])
def test_failed_verification_never_saves_connected(client,samsara_account,monkeypatch,code):
    def fail(*args):raise transport.TmsError(code)
    monkeypatch.setattr(transport,'request',fail)
    for path in ['test','connect']:
        response=post(client,BASE+'/samsara/'+path,body())
        assert response.status_code==400 and TOKEN not in response.text
    with owner_engine.connect() as db:assert db.scalar(text("SELECT count(*) FROM connector_connections WHERE provider='samsara'"))==0

def test_connect_retests_after_successful_test_and_keeps_saved_account_on_failed_replacement(client,samsara_account,monkeypatch):
    assert client.post(BASE+'/samsara/test',json=body()).status_code==200
    monkeypatch.setattr(transport,'request',lambda *a:{'data':{'id':'','name':'Bad'}})
    assert post(client,BASE+'/samsara/connect',body()).status_code==400


def test_existing_account_cannot_be_replaced_and_revoked_token_requires_reconnect(client,samsara_account,monkeypatch):
    row=connect(client)
    monkeypatch.setattr(transport,'request',lambda *a:{'data':{'id':'other','name':'Other Fleet'}})
    assert client.post(BASE+'/samsara/test',json=body(row['version'])).status_code==409
    assert post(client,BASE+'/samsara/connect',body(row['version'])).status_code==409
    def fail(*args):raise transport.TmsError('UNAUTHORIZED')
    monkeypatch.setattr(transport,'request',fail)
    response=post(client,BASE+'/samsara/sync',{'version':row['version']})
    assert response.status_code==200 and response.json()['status']=='RECONNECT_REQUIRED'


def test_other_company_and_shipper_cannot_test_or_connect(client,samsara_account):
    assert client.post(BASE.replace('/acme/','/other/')+'/samsara/test',json=body()).status_code==404
    rates=client.get('/api/v1/companies/acme/rate-cards').json()
    fixed=next(row['id'] for row in rates if row['data']['method']=='FIXED')
    reply=post(client,'/api/v1/companies/acme/shippers',dict(name='Test Shipper',kind='BUSINESS',company_name='Test Shipper',email='samsara-shipper@example.com',phone='6045550198',rate_card_id=fixed,warehouse=dict(text='123 Main Street, Vancouver, BC V5Y 1V4, Canada',city='Vancouver',province='BC',postal_code='V5Y 1V4',latitude=49.26,longitude=-123.11)))
    assert reply.status_code==201,reply.text
    login(client,'customer','acme','samsara-shipper@example.com',reply.json()['initial_password'])
    assert client.post(BASE+'/samsara/test',json=body()).status_code==403
    assert post(client,BASE+'/samsara/connect',body()).status_code==403
    login(client)
    assert client.post(BASE+'/samsara/test',json=body()).status_code==404


def test_custom_host_and_whitespace_tokens_rejected_without_request(client,samsara_account):
    assert post(client,BASE+'/samsara/test',{'credentials':{'api_key':TOKEN,'base_url':'https://attacker.example'}}).status_code==422
    assert post(client,BASE+'/samsara/test',{'credentials':{'api_key':'bad token'}}).status_code==422
    assert not samsara_account
