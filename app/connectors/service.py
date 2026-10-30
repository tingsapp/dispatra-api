"""Transactional connection state and OAuth challenges; public projections never include adapter maps or secrets."""
import base64
import hashlib
import json
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
from fastapi import HTTPException
from sqlalchemy import select
from app.intake import vault
from app.models import now
from app.operations.common import command
from app.services import audit
from . import profiles
from .catalog import BY_ID
from .models import Authorization, Connection, Inbox
from .schemas import ConnectorView
from . import availability, verification, samsara

MESSAGES = {
 'ACTIVATION_REQUIRED':'This connector needs activation by Dispatra before it can connect.',
 'UNAUTHORIZED':'Access has expired or was rejected. Reconnect to continue.',
 'FORBIDDEN':'This connection does not have permission to read the required records.',
 'FORMAT_CHANGED':'The TMS returned a changed data format. Dispatra needs to update the connector.',
 'IDENTITY_NOT_VERIFIED':'The TMS account could not be verified.',
 'ACCOUNT_SELECTION_REQUIRED':'Dispatra needs to confirm which CartonCloud company to connect.',
 'COLLECTION_CHANGED':'The TMS records changed during synchronization. Dispatra will retry the complete import.',
 'CURRENCY_MISMATCH':'The TMS currency does not match this Dispatra workspace.',
}

def require_provider(provider):
    if provider not in BY_ID: raise HTTPException(404,'Connector not found.')
    return BY_ID[provider]

def get(db,user,provider,lock=False):
    query=select(Connection).where(Connection.organization_id==user.organization_id,Connection.provider==provider)
    return db.scalar(query.with_for_update() if lock else query)

def view(db,provider,row=None):
    info=require_provider(provider);ready=availability.ready(provider) and availability.enabled(db,provider)
    status=row.status if row else 'NOT_CONNECTED'
    if not ready and status!='DISCONNECTED':status='ACTIVATION_REQUIRED'
    paused = bool(row and row.paused) or (info.auth != 'request_setup' and not availability.enabled(db,provider))
    if paused: status='PAUSED'
    return ConnectorView(id=provider,name=info.name,initials=info.initials,description=info.description,auth=info.auth,
        ready=ready,status=status,paused=paused,account=row.account_label if row else None,version=row.version if row else None,
        last_sync_at=row.last_sync_at if row else None,
        imported=(row.summary or {}).get('imported',0) if row else 0,review_count=(row.summary or {}).get('review',0) if row else 0,
        message='Synchronization is paused by the administrator.' if paused else 'Account access verified. Fleet data imports are not enabled.' if provider == 'samsara' and status == 'CONNECTED' else MESSAGES['ACTIVATION_REQUIRED'] if not ready else MESSAGES.get(row.last_error,'Synchronization could not finish. Try again later.') if row and row.last_error else None,
        credential_fields=[{'key':key,'label':label,'secret':secret} for key,label,secret in info.credential_fields])

def require_profile(db,provider):
    require_provider(provider); profile=profiles.installed(provider)
    if not availability.enabled(db,provider):raise HTTPException(409,'This connector is paused or awaiting preparation.')
    if profile is None:raise HTTPException(503,MESSAGES['ACTIVATION_REQUIRED'])
    return profile

def origin():
    value=os.environ.get('CONNECTOR_PUBLIC_ORIGIN','').rstrip('/')
    p=urlsplit(value)
    # Must be explicitly configured: Host headers and submitted redirect URLs are not trusted.
    if p.scheme!='https' or not p.hostname or p.path or p.query or p.fragment or p.username:raise HTTPException(503,'Connector authorization is not activated yet.')
    return value

def callback_url(slug,provider):return f'{origin()}/api/v1/companies/{slug}/integrations/connectors/{provider}/callback'

def guard(row,version):
    if (row.version if row else None)!=version:raise HTTPException(409,'Connection changed. Reload before continuing.')
    if row and row.lease:raise HTTPException(409,'Synchronization is running. Try again after it finishes.')

def begin(db,user,slug,provider,data,key):
    info=require_provider(provider)
    if provider == 'samsara':
        if not availability.enabled(db,provider,lock=True): raise HTTPException(409, 'This connector is paused.')
        profile=None
    else: profile=require_profile(db,provider)
    credentials={name:value.get_secret_value() for name,value in data.credentials.items()}
    expected={field[0] for field in info.credential_fields}
    if info.auth=='oauth' and credentials:raise HTTPException(422,'Use the TMS authorization screen for this connection.')
    if info.auth!='oauth' and (set(credentials)!=expected or any(not value or len(value)>2048 for value in credentials.values())):
        raise HTTPException(422,'Enter the connection credentials.')
    # Bound retries to exact credential bytes without persisting those bytes in operation results.
    digest=hashlib.sha256(json.dumps(credentials,sort_keys=True).encode()).hexdigest()
    def run():
        row=get(db,user,provider,True);guard(row,data.version)
        if row and row.paused:raise HTTPException(409,'Synchronization is paused by the administrator.')
        if info.auth=='oauth':
            state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
            challenge=Authorization(organization_id=user.organization_id,user_id=user.id,provider=provider,
                state_hash=hashlib.sha256(state.encode()).hexdigest(),verifier=vault.seal(verifier),
                profile_revision=profile.revision,connection_version=row.version if row else None,expires_at=now()+timedelta(minutes=10))
            db.add(challenge);db.flush()
            proof=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            params={'response_type':'code','client_id':os.environ[profile.client_id_env], 'redirect_uri':callback_url(slug,provider),
                'scope':profile.scopes,'state':state,'code_challenge':proof,'code_challenge_method':'S256'}
            if profile.audience:params['audience']=profile.audience
            audit(db,user,'connector.authorization_started',challenge.id,user.organization_id)
            return {'connector':view(db,provider,row).model_dump(mode='json'),'authorization_url':profile.authorize_url+'?'+urlencode(params)}
        if provider == 'samsara':
            account,label=samsara.identity(credentials);verified=credentials
            if row and row.status != 'DISCONNECTED' and row.account_id and row.account_id != account:
                raise HTTPException(409, 'This token belongs to a different Samsara account. Disconnect before changing accounts.')
        else:
            verified, account, label = verification.verify(provider, profile, credentials, row.account_id if row else None)
        if row is None:
            row=Connection(organization_id=user.organization_id,provider=provider,summary={},version=0)
            db.add(row)
        row.credentials=vault.seal(json.dumps(verified));row.profile_revision=samsara.REVISION if provider == 'samsara' else profile.revision
        row.account_id=account;row.account_label=label
        row.status='CONNECTED';row.last_error=None;row.next_attempt_at=None if provider == 'samsara' else now();row.version+=1
        db.flush()
        audit(db,user,'connector.connection_requested',row.id,user.organization_id);db.flush()
        return {'connector':view(db,provider,row).model_dump(mode='json'),'authorization_url':None}
    return command(db,user,key,'connector-connect:'+provider,{'version':data.version,'credential_digest':digest},run)

def finish(db,user,slug,provider,state,code,error):
    profile=require_profile(db,provider)
    challenge=db.scalar(select(Authorization).where(Authorization.organization_id==user.organization_id,
        Authorization.provider==provider,Authorization.user_id==user.id,
        Authorization.state_hash==hashlib.sha256(state.encode()).hexdigest()).with_for_update())
    if challenge is None or challenge.used or challenge.expires_at<=now() or challenge.profile_revision!=profile.revision:
        raise HTTPException(400,'This connection request expired or was already used. Start again.')
    row=get(db,user,provider,True);guard(row,challenge.connection_version)
    if row and row.paused:raise HTTPException(409,'Synchronization is paused by the administrator.')
    if not error and not code:raise HTTPException(400,'The TMS did not return an authorization code.')
    challenge.used=True
    if error:
        audit(db,user,'connector.authorization_declined',challenge.id,user.organization_id)
        return
    if row is None:
        row=Connection(organization_id=user.organization_id,provider=provider,summary={},version=0);db.add(row)
    # The worker exchanges this one-time code, then verifies the remote identity before Connected is shown.
    row.credentials=vault.seal(json.dumps({'authorization_code':code,'verifier':vault.unseal(challenge.verifier,'connector authorization'),
        'redirect_uri':callback_url(slug,provider)}))
    row.profile_revision=profile.revision;row.status='CONNECTING';row.last_error=None;row.next_attempt_at=now();row.version+=1
    db.flush()
    audit(db,user,'connector.authorization_received',row.id,user.organization_id)

def action(db,user,provider,data,key,disconnect=False):
    require_provider(provider)
    def run():
        row=get(db,user,provider,True)
        if row is None:raise HTTPException(404,'Connection not found.')
        # Disconnect invalidates an in-flight lease; all worker writes check it before committing.
        if row.version!=data.version:raise HTTPException(409,'Connection changed. Reload before continuing.')
        if disconnect:
            row.status='DISCONNECTED';row.credentials=None;row.lease=None;row.lease_until=None;row.next_attempt_at=None;row.last_error=None
        elif provider == 'samsara':
            if row.paused or not availability.enabled(db,provider,lock=True): raise HTTPException(409, 'This connector is paused.')
            if not row.credentials: raise HTTPException(409, 'Reconnect before testing.')
            try:
                account,label=samsara.identity(json.loads(vault.unseal(row.credentials,'connector credentials')))
                if account != row.account_id: raise HTTPException(409, 'The Samsara account changed. Reconnect to continue.')
                row.account_label=label;row.status='CONNECTED';row.last_error=None
            except HTTPException:
                row.status='RECONNECT_REQUIRED';row.last_error='UNAUTHORIZED'
            row.next_attempt_at=None
        else:
            require_profile(db,provider)
            if row.paused:raise HTTPException(409,'Synchronization is paused by the administrator.')
            if not row.credentials:raise HTTPException(409,'Reconnect before synchronizing.')
            if row.lease:raise HTTPException(409,'Synchronization is already running.')
            row.next_attempt_at=now();row.last_error=None
        row.version+=1;audit(db,user,'connector.disconnected' if disconnect else 'connector.sync_requested',row.id,user.organization_id)
        return view(db,provider,row).model_dump(mode='json')
    return command(db,user,key,'connector-disconnect' if disconnect else 'connector-sync',{'provider':provider,'version':data.version},run)
