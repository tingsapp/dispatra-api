from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app.main import app
from app.database import engine, context
from app.models import User, Organization, LoginSession
from app.operations.models import Shipper
from app.security import digest
from conftest import login, company, customer, post, PASSWORD, HEADERS, owner_engine

def test_complete_first_login_profile_persistence(client, accounts):
    me=login(client,'customer','acme','customer')
    assert me['role']=='SHIPPER' and 'password' not in str(me)
    profile=client.get('/api/v1/companies/acme/profile').json()
    assert profile['id']==accounts[2]['id']
    assert profile['contact_name']==''
    response=client.patch('/api/v1/companies/acme/profile',headers={'Idempotency-Key':str(uuid4())},json=dict(version=1,contact_name='Jane',email='jane@example.com',phone='555123',address='123 Main St'))
    assert response.status_code==200, response.text
    assert response.json()['version']==2
    client.post('/api/v1/auth/logout')
    login(client,'customer','acme','customer')
    assert client.get('/api/v1/companies/acme/profile').json()['contact_name']=='Jane'
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action = 'customer.profile_updated'"))==1

def test_cross_company_and_customer_authorization(client, accounts):
    login(client,'customer','acme','customer')
    assert client.get('/api/v1/companies/other/profile').status_code==404
    assert client.get('/api/v1/companies/acme/customers').status_code==403
    assert client.get('/api/v1/platform/organizations').status_code==403
    assert client.get('/api/v1/companies/acme/customers/'+accounts[3]['id']).status_code==404
    assert client.get('/api/v1/companies/acme/profile').json()['id']!=accounts[3]['id']
    for injected in [{'customer_id':accounts[3]['id']},{'organization_id':accounts[1]['id']},{'status':'ACTIVE'},{'role':'DISPATCHER'},{'name':'Stolen'}]:
        response=client.patch('/api/v1/companies/acme/profile',headers={'Idempotency-Key':str(uuid4())},json={'version':1,**injected})
        assert response.status_code==422, response.text
    login(client,'dispatch','acme','dispatcher')
    assert client.get('/api/v1/companies/other/customers').status_code==404
    assert client.post(f"/api/v1/companies/acme/customers/{accounts[4]['id']}/password",json={'password':PASSWORD}).status_code==404

def test_rls_fail_closed_and_transaction_reset(accounts):
    acme,other,first,second,foreign=accounts
    with Session(engine) as db:
        with db.begin():
            assert db.scalars(select(Shipper)).all()==[]
            context(db, acme['id'])
            assert {str(c.id) for c in db.scalars(select(Shipper))}=={first['id'],second['id']}
        db.expunge_all()
        with db.begin():
            assert db.scalars(select(Shipper)).all()==[]
            context(db, acme['id'], shipper_id=first['id'])
            assert [str(c.id) for c in db.scalars(select(Shipper))]==[first['id']]
    with pytest.raises(DBAPIError):
        with Session(engine) as db, db.begin():
            context(db,acme['id'])
            db.add(Shipper(organization_id=other["id"],number="ILLEGAL",name="Leak",warehouse=None))
            db.flush()

def test_foreign_customer_link_rejected_by_database(accounts):
    acme,other,first,second,foreign=accounts
    with pytest.raises(DBAPIError):
        with Session(owner_engine) as db, db.begin():
            db.add(User(organization_id=acme['id'],shipper_id=foreign['id'],scope=acme['id'],login_id='invalid',role='SHIPPER',password_hash='not-a-real-hash'))
            db.flush()

def test_idempotency_and_atomic_uniqueness(client):
    login(client)
    data=dict(name='Acme',slug='acme',admin_login='dispatcher',password=PASSWORD)
    key=str(uuid4())
    first=post(client,'/api/v1/platform/organizations',data,key)
    repeat=post(client,'/api/v1/platform/organizations',data,key)
    assert first.status_code==repeat.status_code==201
    assert first.json()==repeat.json()
    assert post(client,'/api/v1/platform/organizations',{**data,'name':'Changed'},key).status_code==409
    login(client,'dispatch','acme','dispatcher')
    original=customer(client)
    duplicate=post(client,'/api/v1/companies/acme/customers',dict(name='Duplicate',number='C-002',login_id='customer',password=PASSWORD))
    assert duplicate.status_code==409
    assert len(client.get('/api/v1/companies/acme/customers').json())==1
    with owner_engine.connect() as db:
        stored=str(db.execute(text('SELECT result FROM operations')).all())
        assert PASSWORD not in stored
        assert db.scalar(text("SELECT bool_and(payload_hash LIKE '$argon2id$%') FROM operations"))

def test_optimistic_profile_conflict_and_replay(client,accounts):
    login(client,'customer','acme','customer')
    key=str(uuid4()); payload={'version':1,'contact_name':'Jane'}
    first=client.patch('/api/v1/companies/acme/profile',json=payload,headers={'Idempotency-Key':key})
    assert first.status_code==200
    assert client.patch('/api/v1/companies/acme/profile',json=payload,headers={'Idempotency-Key':key}).json()==first.json()
    assert client.patch('/api/v1/companies/acme/profile',json=payload,headers={'Idempotency-Key':str(uuid4())}).status_code==409

def test_password_change_revokes_other_sessions(client,accounts):
    login(client,'customer','acme','customer')
    with TestClient(app,headers=HEADERS) as other:
        login(other,'customer','acme','customer')
        assert client.post('/api/v1/auth/password',json={'current_password':'wrong','new_password':'New-password-123!'}).status_code==400
        response=client.post('/api/v1/auth/password',json={'current_password':PASSWORD,'new_password':'New-password-123!'})
        assert response.status_code==204,response.text
        assert client.get('/api/v1/auth/me').status_code==200
        assert other.get('/api/v1/auth/me').status_code==401
        failed=other.post('/api/v1/auth/login',json=dict(organization='acme',portal='customer',login_id='customer',password=PASSWORD))
        assert failed.status_code==401
        login(other,'customer','acme','customer','New-password-123!')

def test_dispatcher_reset_revokes_customer_sessions(client,accounts):
    with TestClient(app,headers=HEADERS) as cust:
        login(cust,'customer','acme','customer')
        response=client.post(f"/api/v1/companies/acme/customers/{accounts[2]['id']}/password",json={'password':'Reset-password-123!'})
        assert response.status_code==204,response.text
        assert cust.get('/api/v1/auth/me').status_code==401
        login(cust,'customer','acme','customer','Reset-password-123!')

def test_csrf_and_validation_redaction(client):
    response=client.post('/api/v1/auth/login',headers={'Origin':'https://attacker.example'},json=dict(portal='platform',login_id='owner',password=PASSWORD))
    assert response.status_code==403
    response=client.post('/api/v1/auth/login',headers={'Origin':'https://dispatra.vercel.app'},json={})
    assert response.status_code==422  # Official web origin reaches request validation.
    response=client.post('/api/v1/auth/login',json=dict(portal='platform',login_id='owner',password='x'*129))
    assert response.status_code==422
    assert 'x'*129 not in response.text
    assert response.headers['cache-control']=='no-store'
    assert client.post('/api/v1/signup',json={}).status_code==404
    login(client)
    assert 'httponly' in str(client.cookies).lower() or client.cookies.get('dispatra_session')

def test_login_rate_limit_and_wrong_portal(client):
    for i in range(10):
        response=client.post('/api/v1/auth/login',json=dict(portal='platform',login_id='owner',password='wrong'))
        assert response.status_code==401
    assert client.post('/api/v1/auth/login',json=dict(portal='platform',login_id='owner',password=PASSWORD)).status_code==429

def test_disabled_organization_and_session_expiry(client,accounts):
    login(client,'customer','acme','customer')
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET active=false WHERE slug='acme'"))
    assert client.get('/api/v1/auth/me').status_code==401
    with owner_engine.begin() as db:
        db.execute(text("UPDATE organizations SET active=true WHERE slug='acme'"))
        db.execute(text("UPDATE login_sessions SET expires_at=now()-interval '1 second'"))
    assert client.get('/api/v1/auth/me').status_code==401

def test_ready_restricted_role_and_pagination(client,accounts):
    assert client.get('/ready').status_code==200
    first=client.get('/api/v1/companies/acme/customers?limit=1').json()
    second=client.get('/api/v1/companies/acme/customers',params={'limit':1,'after':first[0]['id']}).json()
    assert len(first)==len(second)==1 and first[0]['id']!=second[0]['id']
    assert client.get('/api/v1/companies/acme/customers?limit=101').status_code==422
