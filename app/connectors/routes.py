from typing import Annotated
from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from app.security import rate_limit
from . import service
from .catalog import PROVIDERS, LEGACY_PROVIDERS
from .models import Inbox
from .schemas import ConnectorTestResult, ConnectInput, ConnectResult, ConnectorAction, ConnectorView, ReviewItem

router=APIRouter(prefix='/api/v1/companies/{slug}/integrations/connectors',tags=['Connectors'])
Dispatcher=Annotated[User,Depends(auth.dispatcher)]

@router.get('',response_model=list[ConnectorView])
def list_connectors(slug:str,db:DB,user:Dispatcher):
    rows = []
    for info in (*PROVIDERS, *LEGACY_PROVIDERS):
        saved = service.get(db,user,info.id)
        if info in LEGACY_PROVIDERS and (saved is None or saved.status == 'DISCONNECTED'):
            continue
        row = service.view(db,info.id,saved)
        rows.append(row)
    return rows

@router.post('/{provider}/test',response_model=ConnectorTestResult)
def test_connection(slug:str,provider:str,data:ConnectInput,db:DB,user:Dispatcher):
    from fastapi import HTTPException
    from . import availability, samsara
    rate_limit(f'connector-test:{user.id}',20)
    service.require_provider(provider)
    if provider != 'samsara': raise HTTPException(409,'Separate connection testing is not available for this provider.')
    if not availability.enabled(db,provider,lock=True): raise HTTPException(409,'This connector is paused.')
    row=service.get(db,user,provider)
    if row and row.paused: raise HTTPException(409,'This connector is paused.')
    service.guard(row,data.version)
    account,label=samsara.identity({name:value.get_secret_value() for name,value in data.credentials.items()})
    if row and row.status != 'DISCONNECTED' and row.account_id and row.account_id != account:
        raise HTTPException(409,'This token belongs to a different Samsara account. Disconnect before changing accounts.')
    return {'account_id':account,'account':label}

@router.post('/{provider}/connect',response_model=ConnectResult)
def connect(slug:str,provider:str,data:ConnectInput,db:DB,key:OperationKey,user:Dispatcher):
    rate_limit(f'connector-connect:{user.id}',20)
    return service.begin(db,user,slug,provider,data,key)

@router.get('/{provider}/callback',response_class=RedirectResponse)
def callback(slug:str,provider:str,db:DB,user:Dispatcher,state:Annotated[str,Query(min_length=20,max_length=200)],
    code:Annotated[str,Query(max_length=4096)]='',error:Annotated[str,Query(max_length=200)]=''):
    service.finish(db,user,slug,provider,state,code,error)
    return RedirectResponse(service.origin()+f'/{slug}/integrations',status_code=303,headers={'Referrer-Policy':'no-referrer'})

@router.post('/{provider}/sync',response_model=ConnectorView)
def sync(slug:str,provider:str,data:ConnectorAction,db:DB,key:OperationKey,user:Dispatcher):
    rate_limit(f'connector-sync:{user.organization_id}:{provider}',10)
    return service.action(db,user,provider,data,key)

@router.post('/{provider}/disconnect',response_model=ConnectorView)
def disconnect(slug:str,provider:str,data:ConnectorAction,db:DB,key:OperationKey,user:Dispatcher):
    return service.action(db,user,provider,data,key,True)

@router.get('/{provider}/review',response_model=list[ReviewItem])
def review(slug:str,provider:str,db:DB,user:Dispatcher):
    service.require_provider(provider);connection=service.get(db,user,provider)
    if connection is None:return []
    rows=db.scalars(select(Inbox).where(Inbox.organization_id==user.organization_id,Inbox.connection_id==connection.id,
        Inbox.status=='NEEDS_REVIEW').order_by(Inbox.created_at.desc()).limit(50))
    return [{'kind':row.kind,'id':str(row.id),'reference':row.external_id,'reasons':row.reasons,'created_at':row.created_at} for row in rows]
