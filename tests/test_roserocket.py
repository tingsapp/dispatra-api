from datetime import timedelta
from urllib.parse import parse_qs,urlsplit
import json
import pytest
from test_connectors import configured,profile,shipment,check,BASE
from conftest import post,owner_engine
from app.connectors import roserocket,profiles,transport,worker
from app.integrations import client as http
from app.models import now
from app.intake import vault
from sqlalchemy import text


def resource():
 row=roserocket.orders_resource('reviewed-board',['customer','stops','commodities'],'America/Vancouver')
 row.page_size=1
 return row


def test_board_import_reads_all_pages_and_expands_relationships(configured,monkeypatch):
 p=profiles.Profile.model_validate(profile('rose-rocket'));r=resource();calls=[]
 def request(method,url,headers,body=None):
  calls.append((method,url,body))
  if method=='POST':return {'total':2,'results':[{'id':str(body['offset']),'objectKey':'order','orgId':'account-1'}]}
  return {'id':urlsplit(url).path.rsplit('/',1)[1],'orgId':'account-1','objectKey':'order','customer':{'email':'shipper@example.test'},'stops':[{},{}]}
 monkeypatch.setattr(http,'request',request)
 rows=transport.fetch('rose-rocket',p,r,{'access_token':'sample','account_id':'account-1'})
 assert len(rows)==2 and [c[2]['offset'] for c in calls if c[0]=='POST']==[0,1]
 assert all(parse_qs(urlsplit(c[1]).query)['paths']==['customer,stops,commodities'] for c in calls if c[0]=='GET')
 assert rows[0]['customer']['email']=='shipper@example.test'


@pytest.mark.parametrize('mode,error',[('wrong-company','ACCOUNT_CHANGED'),('wrong-detail','ACCOUNT_CHANGED'),('repeat','PAGINATION_LOOP'),('changing-total','COLLECTION_CHANGED'),('empty-page','FORMAT_CHANGED')])
def test_board_import_refuses_wrong_company_and_truncated_or_unstable_pages(configured,monkeypatch,mode,error):
 p=profiles.Profile.model_validate(profile('rose-rocket'));r=resource()
 def request(method,url,headers,body=None):
  if method=='POST':
   index=body['offset']
   return {'total':3 if mode=='changing-total' and index else 2,'results':[] if mode=='empty-page' else [{'id':'0' if mode=='repeat' else str(index),'objectKey':'order','orgId':'wrong' if mode=='wrong-company' else 'account-1'}]}
  return {'id':'wrong' if mode=='wrong-detail' else urlsplit(url).path.rsplit('/',1)[1],'objectKey':'order','orgId':'account-1'}
 monkeypatch.setattr(http,'request',request)
 with pytest.raises(transport.Error,match=error):transport.fetch('rose-rocket',p,r,{'access_token':'sample','account_id':'account-1'})


def test_token_refresh_is_due_based_and_keeps_company_scope(configured,monkeypatch):
 p=profiles.Profile.model_validate(profile('rose-rocket'));calls=[]
 def token(profile,values):calls.append(values);return {'access_token':'renewed','refresh_token':'rotated','expires_in':3600}
 monkeypatch.setattr(transport,'token',token)
 credentials={'access_token':'current','refresh_token':'refresh','account_id':'company-one','expires_at':(now()+timedelta(minutes=10)).isoformat()}
 assert worker.authorize(p,credentials)==credentials and not calls
 updated=worker.authorize(p,credentials,force=True)
 assert calls[0]['org_id']=='company-one' and updated['refresh_token']=='rotated' and updated['expires_at']!=credentials['expires_at']


def test_auth_code_is_not_reused_after_failed_remote_identity(client,configured,monkeypatch):
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'single-use'},follow_redirects=False)
 with owner_engine.connect() as db:org,identity=db.execute(text("SELECT organization_id,id FROM connector_connections WHERE provider='rose-rocket'")).one()
 monkeypatch.setattr(http,'request',lambda *args,**kwargs: (_ for _ in ()).throw(transport.Error('CONNECTION_FAILED')))
 assert not worker.sync(org,identity)
 with owner_engine.connect() as db:
  saved=json.loads(vault.unseal(db.scalar(text('SELECT credentials FROM connector_connections WHERE id=:id'),{'id':identity})))
 assert 'authorization_code' not in saved and saved['access_token']=='test-token'


def test_unauthorized_read_renews_once_and_imports_without_duplicates(client,configured,monkeypatch):
 configured['rows'].append(shipment())
 reply=check(post(client,BASE+'/rose-rocket/connect',{}));state=parse_qs(urlsplit(reply['authorization_url']).query)['state'][0]
 client.get(BASE+'/rose-rocket/callback',params={'state':state,'code':'single-use'},follow_redirects=False)
 with owner_engine.connect() as db:org,identity=db.execute(text("SELECT organization_id,id FROM connector_connections WHERE provider='rose-rocket'")).one()
 original=http.request;failed=False;tokens=[]
 def request(*args,**kwargs):
  nonlocal failed
  if not failed:failed=True;raise transport.Error('UNAUTHORIZED')
  return original(*args,**kwargs)
 def token(profile,values,*args):tokens.append(values['grant_type']);return {'access_token':'fresh','refresh_token':'rotated','expires_in':3600}
 monkeypatch.setattr(http,'request',request);monkeypatch.setattr(transport,'token',token)
 assert worker.sync(org,identity)
 assert tokens==['authorization_code','refresh_token']
 assert next(row for row in check(client.get(BASE)) if row['id']=='rose-rocket')['imported']==1
