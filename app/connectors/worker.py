"""Run `python -m app.connectors.worker`; isolated from the existing manual TMS worker.

Leases serialize each connection across processes. Disconnect/reconnect invalidates outstanding commits. Network
requests are read-only imports or explicitly translated status writes; payloads and credentials never reach logs.
"""
import argparse
import json
import time
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from app.database import context,engine
from app.events import feed
from app.intake import vault
from app.integrations import client,fleet
from app.models import Organization,now
from . import imports,profiles,transport
from .catalog import BY_ID
from .models import Connection,Link,Inbox
from .availability import enabled

LEASE_MINUTES=10

def claim(org,identity):
    with Session(engine) as db,db.begin():
        context(db,org)
        row=db.scalar(select(Connection).where(Connection.organization_id==org,Connection.id==identity).with_for_update(skip_locked=True))
        if row is None or not row.credentials or row.paused or not enabled(db,row.provider) or row.status in ['DISCONNECTED','RECONNECT_REQUIRED']:return None
        if row.lease and row.lease_until and row.lease_until>now():return None
        if row.next_attempt_at and row.next_attempt_at>now():return None
        if not db.scalar(select(Organization.id).where(Organization.id==org,Organization.active)):return None
        lease=uuid4();prior=row.status;row.lease=lease;row.lease_until=now()+timedelta(minutes=LEASE_MINUTES)
        row.status='SYNCING';row.last_attempt_at=now();row.version+=1
        return {'provider':row.provider,'credentials':vault.unseal(row.credentials,'connector credentials'),
            'revision':row.profile_revision,'account':row.account_id,'lease':lease,'prior':prior}

def authorize(profile,credentials,force=False):
    if 'authorization_code' in credentials:
        result=transport.token(profile,{'grant_type':'authorization_code','code':credentials['authorization_code'],
            'code_verifier':credentials['verifier'],'redirect_uri':credentials['redirect_uri']})
        return {'access_token':result['access_token'],'refresh_token':result.get('refresh_token'),
            'expires_at':(now()+timedelta(seconds=int(result.get('expires_in',3600)))).isoformat()}
    if BY_ID[profile.provider].auth=='client_credentials':
        result=transport.token(profile,{'grant_type':'client_credentials'},credentials)
        return {**credentials,'access_token':result['access_token']}
    if credentials.get('refresh_token'):
        try:expiry=datetime.fromisoformat(credentials.get('expires_at',''))
        except (ValueError,TypeError):expiry=None
        if not force and expiry and expiry.tzinfo and expiry>now()+timedelta(seconds=60):return credentials
        result=transport.token(profile,{'grant_type':'refresh_token','refresh_token':credentials['refresh_token'],
            **({'org_id':credentials['account_id']} if profile.provider=='rose-rocket' and credentials.get('account_id') else {})})
        return {**credentials,'access_token':result['access_token'],'refresh_token':result.get('refresh_token') or credentials['refresh_token'],
            'expires_at':(now()+timedelta(seconds=int(result.get('expires_in',3600)))).isoformat()}
    return credentials

def save_tokens(org,identity,lease,credentials):
    # Save rotated refresh tokens even if subsequent account/data reads fail.
    with Session(engine) as db,db.begin():
        context(db,org);row=imports.current(db,org,identity,lease)
        row.credentials=vault.seal(json.dumps(credentials))

def remote_records(org,identity,lease,profile,credentials):
    for attempt in range(2):
        save_tokens(org,identity,lease,credentials)
        try:
            account,label=transport.identity(profile.provider,profile,credentials)
            credentials={**credentials,'account_id':account}
            ctx=persist_auth(org,identity,lease,profile,credentials,account,label)
            source={kind:transport.fetch(profile.provider,profile,resource,credentials) for kind,resource in profile.resources.items()}
            return credentials,ctx,source
        except transport.Error as failure:
            if attempt or failure.code!='UNAUTHORIZED' or not credentials.get('refresh_token'):raise
            credentials=authorize(profile,credentials,force=True)

def persist_auth(org,identity,lease,profile,credentials,account,label):
    with Session(engine) as db,db.begin():
        context(db,org);row=imports.current(db,org,identity,lease)
        if row.account_id and row.account_id!=account:raise transport.Error('ACCOUNT_CHANGED')
        row.account_id=account;row.account_label=label;row.credentials=vault.seal(json.dumps({**credentials,'account_id':account}))
        # Initialize independent status delivery from the connection's first verified authorization.
        if row.event_cursor is None:row.event_cursor=feed.head(db)
        return imports.setup(db,org,profile)

def deliver(org,identity,lease,profile,credentials):
    if not profile.status_path:return 0
    with Session(engine) as db,db.begin():
        context(db,org);row=imports.current(db,org,identity,lease)
        rows,_=feed.read(db,org,feed.parse(db,row.event_cursor) or feed.parse(db,feed.head(db)),100)
        work=[]
        from app.operations.models import Order
        for event in rows:
            link=db.scalar(select(Link).where(Link.organization_id==org,Link.connection_id==identity,Link.kind=='ORDER',Link.record_id==event['order_id'])) if event['order_id'] else None
            order=db.get(Order,link.record_id) if link else None
            value=profile.status_values.get(order.status) if order else None
            work.append((f"{event['tx']}-{event['seq']}",str(event['id']),link.external_id if link else None,value))
    from urllib.parse import quote
    sent=0
    for position,event_id,external,value in work:
        # Unknown vendor statuses are not guessed or sent.
        if external and value:
            with Session(engine) as db,db.begin():context(db,org);imports.current(db,org,identity,lease)
            path=profile.status_path.replace('{id}',quote(external,safe=''))
            target=transport.target(profile,profiles.Resource(path=path),credentials)
            client.request(profile.status_method,target,{**transport.headers(profile.provider,profile,credentials),'Idempotency-Key':event_id},
                {profile.status_field:value});sent+=1
        with Session(engine) as db,db.begin():
            context(db,org);row=imports.current(db,org,identity,lease);row.event_cursor=position
    return sent

def sync(org,identity):
    job=claim(org,identity)
    if job is None:return False
    lease=job['lease'];profile=profiles.installed(job['provider']);result={'imported':0,'review':0,'unchanged':0,'sent':0};error=None;credentials=None
    try:
        if profile is None or profile.revision!=job['revision']:raise transport.Error('ACTIVATION_REQUIRED')
        credentials=authorize(profile,json.loads(job['credentials']))
        # Complete pagination before writing; truncated/failed fetches never imply removals.
        credentials,ctx,source=remote_records(org,identity,lease,profile,credentials)
        for kind in ['vehicle','driver','order']:
            for record in source.get(kind,[]):
                try:
                    if kind=='order':outcome=imports.order_record(org,identity,lease,profile,record,ctx)
                    else:outcome='imported' if imports.fleet_record(org,identity,lease,profile,kind,record,ctx) else 'unchanged'
                    result[outcome]+=1
                except (fleet.Skip,ValueError,HTTPException):
                    imports.review_record(org,identity,lease,profile,kind,record)
                    result['review']+=1
        result['sent']=deliver(org,identity,lease,profile,credentials)
    except transport.Error as failure:error=failure.code
    except HTTPException as failure:error='CURRENCY_MISMATCH' if failure.detail=='CURRENCY_MISMATCH' else 'SYNC_FAILED'
    except imports.Stale:return False
    except Exception:error='SYNC_FAILED'
    with Session(engine) as db,db.begin():
        context(db,org)
        try:row=imports.current(db,org,identity,lease)
        except imports.Stale:return False
        result['review']=db.scalar(select(func.count()).select_from(Inbox).where(Inbox.organization_id==org,Inbox.connection_id==identity,Inbox.status=='NEEDS_REVIEW'))
        row.lease=None;row.lease_until=None;row.summary=result;row.last_error=error;row.version+=1
        row.status='RECONNECT_REQUIRED' if error in ['UNAUTHORIZED','ACCOUNT_CHANGED'] else 'SYNC_FAILED' if error else 'CONNECTED'
        if not error:row.last_sync_at=now()
        row.next_attempt_at=None if row.status=='RECONNECT_REQUIRED' else now()+timedelta(minutes=profile.sync_minutes if profile and not error else 15)
    return error is None

from fastapi import HTTPException

def cycle():
    with Session(engine) as db,db.begin():
        context(db,platform=True)
        due=list(db.execute(select(Connection.organization_id,Connection.id).join(Organization,Organization.id==Connection.organization_id).where(
            Connection.credentials.is_not(None),Connection.paused.is_(False),Connection.status.not_in(['DISCONNECTED','RECONNECT_REQUIRED']),Organization.active,
            (Connection.next_attempt_at<=now()) | Connection.next_attempt_at.is_(None))).all())
    for org,identity in due:
        try:sync(org,identity)
        except Exception:print('Connector cycle failed; retrying later.',flush=True)
    return len(due)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    while True:
        count=cycle()
        if args.once:print(f'Checked {count} connector connection(s).');return
        time.sleep(30)

if __name__=='__main__':main()
