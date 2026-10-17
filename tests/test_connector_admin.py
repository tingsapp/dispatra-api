from urllib.parse import parse_qs,urlsplit
from uuid import uuid4
from sqlalchemy import text
from conftest import login,post,owner_engine
from test_connectors import configured,connect_truckmate,connection,check,BASE
from app.connectors import worker

ADMIN='/api/v1/platform/integrations'

def rose_connection(client):
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 assert client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'synthetic'},follow_redirects=False).status_code==303
 with owner_engine.connect() as db:return db.execute(text("SELECT organization_id,id FROM connector_connections WHERE provider='rose-rocket'")).one()

def count(client,provider='rose-rocket'):
 return next(p['connected_dispatchers'] for p in check(client.get(ADMIN+'/providers')) if p['id']==provider)

def test_owner_cards_are_read_only_secret_free_and_dispatcher_scoped(client,configured):
 connect_truckmate(client);org,identity=connection();assert worker.sync(org,identity)
 login(client);rows=check(client.get(ADMIN+'/providers'))
 assert [p['id'] for p in rows[:5]]==['rose-rocket','cartoncloud','truckbase','tailwind','ascendtms']
 assert all(set(p)=={'id','name','initials','connected_dispatchers'} for p in rows)
 assert count(client,'truckmate')==1
 check(client.patch(ADMIN+'/providers/rose-rocket',json={'version':1,'enabled':False},headers={'Idempotency-Key':str(uuid4())}),404)
 check(client.get(ADMIN+'/connections'),404);check(client.get(ADMIN+'/setup-requests'),404)
 login(client,'dispatch','acme','dispatcher');check(client.get(ADMIN+'/providers'),403)
 check(post(client,BASE+'/truckbase/setup-request',{}),404)
 assert '/api/v1/companies/{slug}/integrations/connectors/{provider}/setup-request' not in client.get('/openapi.json').json()['paths']

def test_counts_require_verified_active_connections_and_active_dispatchers(client,configured):
 org,identity=rose_connection(client);login(client)
 assert count(client)==0
 assert worker.sync(org,identity);assert count(client)==1
 with owner_engine.begin() as db:db.execute(text("UPDATE connector_connections SET status='SYNCING' WHERE id=:id"),{'id':identity})
 assert count(client)==1
 with owner_engine.begin() as db:db.execute(text('UPDATE connector_connections SET last_sync_at=NULL WHERE id=:id'),{'id':identity})
 assert count(client)==0
 with owner_engine.begin() as db:db.execute(text("UPDATE connector_connections SET status='CONNECTED',paused=true WHERE id=:id"),{'id':identity})
 assert count(client)==0
 with owner_engine.begin() as db:
  db.execute(text('UPDATE connector_connections SET paused=false WHERE id=:id'),{'id':identity})
  db.execute(text("UPDATE users SET active=false WHERE organization_id=:org AND role='DISPATCHER'"),{'org':org})
 assert count(client)==0
 with owner_engine.begin() as db:
  db.execute(text("UPDATE users SET active=true WHERE organization_id=:org AND role='DISPATCHER'"),{'org':org})
  db.execute(text('UPDATE organizations SET active=false WHERE id=:org'),{'org':org})
 assert count(client)==0
 with owner_engine.begin() as db:db.execute(text('UPDATE organizations SET active=true WHERE id=:org'),{'org':org})
 assert count(client)==1

def test_catalogue_shows_configuration_status_and_preserves_saved_account_disconnection(client,configured,monkeypatch):
 initial=check(client.get(BASE));assert [r['id'] for r in initial]==['rose-rocket','cartoncloud','truckbase','tailwind','ascendtms']
 assert [r['ready'] for r in initial]==[True,True,False,False,False]
 connect_truckmate(client);monkeypatch.delenv('TMS_CONNECTOR_PROFILES_FILE')
 rows=check(client.get(BASE));assert len(rows)==6 and all(not r['ready'] for r in rows)
 saved=next(r for r in rows if r['id']=='truckmate')
 check(post(client,BASE+'/truckmate/disconnect',{'version':saved['version']}))
 assert len(check(client.get(BASE)))==5
 login(client);assert all(p['connected_dispatchers']==0 for p in check(client.get(ADMIN+'/providers')))
