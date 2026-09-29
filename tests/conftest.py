import os
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Tests require a disposable migrated PostgreSQL database, never SQLite or production.
owner_url = os.environ['TEST_OWNER_DATABASE_URL']
assert owner_url.rsplit('/',1)[-1].endswith('_test'), 'Use a database named *_test'
os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']
os.environ['COOKIE_SECURE'] = 'false'
os.environ['ROUTING_PROVIDER'] = 'demo'
os.environ['WEB_ORIGINS'] = 'http://testserver,http://127.0.0.1:3000'
from app.main import app
from app.models import User
from app.security import hash_password
owner_engine = create_engine(owner_url)
HEADERS = {'Origin':'http://testserver','X-Requested-With':'Dispatra'}
PASSWORD = 'Initial-password-123!'

@pytest.fixture(autouse=True)
def reset_database():
    with owner_engine.begin() as c:
        c.execute(text('TRUNCATE organizations, shippers, users, login_sessions, audit_events, operations, login_buckets CASCADE'))
    with Session(owner_engine) as db, db.begin():
        db.add(User(scope='platform',login_id='owner',role='ADMIN',password_hash=hash_password(PASSWORD)))

@pytest.fixture
def client():
    with TestClient(app, headers=HEADERS) as client: yield client

def login(client, portal='platform', organization=None, login_id='owner', password=PASSWORD):
    response = client.post('/api/v1/auth/login', json=dict(portal=portal, organization=organization, login_id=login_id, password=password))
    assert response.status_code == 200, response.text
    return response.json()

def post(client, path, body, key=None):
    return client.post(path,json=body,headers={'Idempotency-Key':key or str(uuid4())})

def company(client, slug='acme'):
    response = post(client,'/api/v1/platform/organizations',dict(name=slug.title(),slug=slug,admin_login='dispatcher',password=PASSWORD))
    assert response.status_code == 201, response.text
    return response.json()

def customer(client, slug='acme', number='C-001', login_id='customer'):
    response = post(client,f'/api/v1/companies/{slug}/customers',dict(name='ABC Trading',number=number,login_id=login_id,password=PASSWORD))
    assert response.status_code == 201, response.text
    return response.json()

@pytest.fixture
def accounts(client):
    login(client)
    acme=company(client)
    other=company(client,'other')
    login(client,'dispatch','other','dispatcher')
    foreign=customer(client,'other')
    login(client,'dispatch','acme','dispatcher')
    first=customer(client)
    second=customer(client,number='C-002',login_id='customer-two')
    return acme,other,first,second,foreign
