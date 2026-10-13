from uuid import uuid4
import pytest
from sqlalchemy import text, select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine, context
from app.models import Organization, EmailVerification
from app.intake import vault
from app import verification_worker
from conftest import post, owner_engine, HEADERS, PASSWORD

pytestmark = pytest.mark.usefixtures('verification_mail')
BODY = dict(slug='verify-trial', name='Verify Trial', full_name='Dispatcher', email='verify@example.com', password=PASSWORD)

def begin(client):
    response = post(client, '/api/v1/auth/signup', BODY)
    assert response.status_code == 201, response.text
    assert response.json()['verification_required']
    with owner_engine.connect() as db:
        row = db.execute(text('SELECT id,organization_id,secret FROM email_verifications')).one()
    return row.id, row.organization_id, vault.unseal(row.secret)

def confirm(client, code, key=None): return post(client, '/api/v1/auth/verification/confirm', {'code':code}, key)

def test_pending_trial_has_no_session_and_cannot_bypass_verification(client):
    _,org,code = begin(client)
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.get('/api/v1/companies/verify-trial/settings').status_code == 401
    login=client.post('/api/v1/auth/login',json=dict(portal='dispatch',login_id=BODY['email'],password=PASSWORD))
    assert login.json()['verification_required']
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM login_sessions')) == 0
        assert db.scalar(text('SELECT trial_started_at FROM organizations')) is None
    with Session(engine) as db,db.begin():
        context(db,org)
        assert not db.get(Organization,org).active
        assert db.scalars(select(Organization).where(Organization.active.is_(True))).all()==[]
    response=confirm(client,code)
    assert response.status_code==200,response.text
    assert response.json()['organization']['active']
    assert client.get('/api/v1/auth/me').status_code==200
    assert client.get('/api/v1/companies/verify-trial/settings').status_code==200
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT consumed_at IS NOT NULL AND secret IS NULL FROM email_verifications'))
        assert db.scalar(text("SELECT trial_ends_at-trial_started_at=interval '30 days' FROM organizations"))

def test_code_attempts_persist_and_lock_out(client):
    _,_,code=begin(client)
    wrong='999999' if code!='999999' else '888888'
    for _ in range(5): assert confirm(client,wrong).status_code==400
    assert confirm(client,code).status_code==429
    with owner_engine.connect() as db: assert db.scalar(text('SELECT attempts FROM email_verifications'))==5
    assert client.get('/api/v1/auth/me').status_code==401

def test_expiry_resend_cooldown_old_code_and_replay(client):
    old,_,code=begin(client)
    path='/api/v1/auth/verification/resend'
    assert post(client,path,{}).status_code==429
    with owner_engine.begin() as db: db.execute(text("UPDATE email_verifications SET expires_at=now()-interval '1 second',resend_at=now()-interval '1 second'"))
    assert confirm(client,code).status_code==400
    key=str(uuid4()); first=post(client,path,{},key)
    assert first.status_code==200,first.text
    assert post(client,path,{},key).json()==first.json()
    with owner_engine.connect() as db:
        assert db.scalar(text('SELECT consumed_at IS NOT NULL FROM email_verifications WHERE id=:id'),{'id':old})
        secret=db.scalar(text('SELECT secret FROM email_verifications WHERE consumed_at IS NULL'))
    new=vault.unseal(secret)
    assert confirm(client,new).status_code==200
    assert client.get('/api/v1/auth/verification').status_code==401

def test_code_is_private_and_cookie_is_not_authentication(client,monkeypatch):
    _,_,code=begin(client)
    sent=[];monkeypatch.setattr(verification_worker.smtp_transport,'send',lambda message:sent.append(message))
    assert verification_worker.poll_once()==1
    assert code in sent[0].body_text
    assert verification_worker.poll_once()==0
    status=client.get('/api/v1/auth/verification')
    assert status.json()['delivery_status']=='SENT' and code not in status.text
    with Session(engine) as db,db.begin(): assert db.scalars(select(EmailVerification)).all()==[]
    with TestClient(app,headers=HEADERS) as stranger:
        assert stranger.get('/api/v1/auth/verification').status_code==401
        assert confirm(stranger,code).status_code==401

@pytest.mark.parametrize('failure,status', [('TemporaryFailure','PENDING'),('PermanentFailure','FAILED'),('UnknownAcceptance','UNKNOWN')])
def test_email_failures_do_not_enable_workspace(client,monkeypatch,failure,status):
    begin(client)
    def fail(message): raise getattr(verification_worker.smtp_transport,failure)('SMTP_TEST')
    monkeypatch.setattr(verification_worker.smtp_transport,'send',fail)
    verification_worker.poll_once()
    assert client.get('/api/v1/auth/verification').json()['delivery_status']==status
    assert client.get('/api/v1/auth/me').status_code==401

def test_code_cannot_be_reused_for_new_session(client):
    _,_,code=begin(client)
    token=client.cookies.get('dispatra_verification')
    assert confirm(client,code).status_code==200
    client.cookies.clear();client.cookies.set('dispatra_verification',token)
    assert confirm(client,code).status_code==409
    assert client.get('/api/v1/auth/me').status_code==401

def test_missing_mail_configuration_rolls_back_signup(client,monkeypatch):
    monkeypatch.delenv('SMTP_HOST')
    assert post(client,'/api/v1/auth/signup',BODY).status_code==503
    with owner_engine.connect() as db: assert db.scalar(text('SELECT count(*) FROM organizations'))==0

def test_concurrent_code_consumption_grants_one_session(client):
    from concurrent.futures import ThreadPoolExecutor
    _,_,code=begin(client);token=client.cookies.get('dispatra_verification')
    def attempt(_):
        with TestClient(app,headers=HEADERS) as browser:
            browser.cookies.set('dispatra_verification',token)
            return confirm(browser,code).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,[1,2]))
    assert sorted(results)==[200,409]
    with owner_engine.connect() as db: assert db.scalar(text('SELECT count(*) FROM login_sessions'))==1

def test_password_rotation_invalidates_pending_verification(client):
    from app.security import hash_password
    _,_,code=begin(client)
    changed='Changed-password-123!'
    with owner_engine.begin() as db: db.execute(text('UPDATE users SET password_hash=:password WHERE login_id=:email'),{'password':hash_password(changed),'email':BODY['email']})
    assert confirm(client,code).status_code==403
    response=client.post('/api/v1/auth/login',json=dict(portal='dispatch',login_id=BODY['email'],password=changed))
    assert response.json()['verification_required']
    with owner_engine.connect() as db: fresh=vault.unseal(db.scalar(text('SELECT secret FROM email_verifications WHERE consumed_at IS NULL')))
    assert confirm(client,fresh).status_code==200
