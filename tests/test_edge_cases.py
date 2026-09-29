from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import text
from conftest import login, company, post, PASSWORD, HEADERS, owner_engine
from app.main import app

def test_customer_cannot_authenticate_in_dispatch_portal(client, accounts):
    for org, portal in [('missing','customer'),('acme','dispatch')]:
        response = client.post('/api/v1/auth/login', json=dict(organization=org,portal=portal,login_id='customer',password=PASSWORD))
        assert response.status_code == 401
    assert client.post('/api/v1/auth/login',json=dict(organization='acme',portal='platform',login_id='owner',password=PASSWORD)).status_code == 401

def test_concurrent_customer_create_replay(client, accounts):
    key = str(uuid4())
    body = dict(name='Concurrent',number='C-003',login_id='concurrent',password=PASSWORD)
    cookies = dict(client.cookies)
    def request():
        with TestClient(app,headers=HEADERS,cookies=cookies) as parallel:
            return post(parallel,'/api/v1/companies/acme/customers',body,key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(),range(2)))
    assert [r.status_code for r in results] == [201,201]
    assert results[0].json() == results[1].json()
    with owner_engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM shippers WHERE number='C-003'"))==1
        assert db.scalar(text("SELECT count(*) FROM outbox_events WHERE entity_id=:id AND event_type='customer.created'"),{'id':results[0].json()['id']})==1

def test_password_spaces_preserved_and_secure_cookie(client, accounts):
    login(client,'customer','acme','customer')
    value='  Long passphrase 123  '
    assert client.post('/api/v1/auth/password',json={'current_password':PASSWORD,'new_password':value}).status_code==204
    client.post('/api/v1/auth/logout')
    response=client.post('/api/v1/auth/login',json=dict(organization='acme',portal='customer',login_id='customer',password=value))
    assert response.status_code==200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert response.headers['cache-control']=='no-store'


def test_patch_preserves_omitted_profile_fields(client, accounts):
    login(client,'customer','acme','customer')
    response=client.patch('/api/v1/companies/acme/profile',headers={'Idempotency-Key':str(uuid4())},json={'version':1,'email':'retained@example.com'})
    assert response.status_code==200
    response=client.patch('/api/v1/companies/acme/profile',headers={'Idempotency-Key':str(uuid4())},json={'version':2,'contact_name':'Changed'})
    assert response.status_code==200
    assert response.json()['email']=='retained@example.com'
