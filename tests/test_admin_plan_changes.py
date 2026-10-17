"""Plan switches are versioned owner commands; billing terms are retained."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from conftest import HEADERS, company, login, owner_engine

ROOT='/api/v1/platform/organizations'


def setup_company(client):
    login(client);org=company(client)
    return org['id'],client.get(f"{ROOT}/{org['id']}").json()


def body(detail,external):
    return dict(version=detail['version'],name=detail['name'],active=detail['workspace_enabled'],settings_version=detail['settings_version'],tms_workspace=external)


def save(client,ident,data,key=None):
    return client.patch(f'{ROOT}/{ident}/overview',json=data,headers={'Idempotency-Key':key or str(uuid4())})


def test_owner_switches_both_directions_restoring_rates_with_safe_replays_and_unchanged_billing(client):
    ident,detail=setup_company(client)
    billing=client.get(f'{ROOT}/{ident}/subscription').json()
    with owner_engine.connect() as db:
        original=db.execute(text('SELECT id::text FROM rate_cards WHERE organization_id=:id AND is_default'),{'id':ident}).scalar_one()
    data=body(detail,True);key=str(uuid4())
    result=save(client,ident,data,key)
    assert result.status_code==200,result.text
    changed=result.json()['company'];assert changed['tms_workspace'] is True
    assert changed['settings_version']==detail['settings_version']+1
    assert save(client,ident,data,key).json()==result.json()
    assert save(client,ident,{**data,'tms_workspace':False},key).status_code==409
    with owner_engine.connect() as db:
        method=db.execute(text("SELECT data->>'method' FROM rate_cards WHERE organization_id=:id AND is_default"),{'id':ident}).scalar_one()
        assert method=='IMPORTED'
    with TestClient(app,headers=HEADERS) as desk:
        login(desk,'dispatch','acme','dispatcher')
        assert desk.get('/api/v1/auth/me').json()['tms_workspace'] is True
    result=save(client,ident,body(changed,False));assert result.status_code==200,result.text
    final=result.json()['company'];assert final['tms_workspace'] is False
    assert final['settings_version']==detail['settings_version']+2
    with owner_engine.connect() as db:
        assert db.execute(text('SELECT id::text FROM rate_cards WHERE organization_id=:id AND is_default'),{'id':ident}).scalar_one()==original
    assert client.get(f'{ROOT}/{ident}/subscription').json()==billing
    assert client.get(ROOT).json()[0]['tms_workspace'] is False


from app.main import app


def test_stale_settings_reject_all_edits_and_missing_version_is_not_accepted(client):
    ident,detail=setup_company(client)
    account,=client.get(f'{ROOT}/{ident}/dispatchers').json()
    for missing in [False,True]:
        data={**body(detail,True),'name':'Unsaved company','phone':'6045550100','dispatcher':{'id':account['id'],'version':account['version'],'login_id':'changed@example.com','display_name':'Changed'}}
        if missing:data.pop('settings_version')
        else:data['settings_version']+=1
        result=save(client,ident,data)
        assert result.status_code==(422 if missing else 409),result.text
        assert client.get(f'{ROOT}/{ident}').json()==detail
        assert client.get(f'{ROOT}/{ident}/dispatchers').json()==[account]


@pytest.mark.parametrize('value',[None,'true',1])
def test_switch_requires_boolean_and_owner(client,value):
    ident,detail=setup_company(client)
    assert save(client,ident,{**body(detail,True),'tms_workspace':value}).status_code==422
    with TestClient(app,headers=HEADERS) as anonymous:
        assert save(anonymous,ident,body(detail,True)).status_code==401
    login(client,'dispatch','acme','dispatcher')
    assert save(client,ident,body(detail,True)).status_code==403


def test_omitted_plan_preserves_external_mode_and_explicit_false_is_payload_bound(client):
    from app.platform.schemas import CompanyOverviewUpdate
    from app.platform.service import company_payload
    ident,detail=setup_company(client)
    switched=save(client,ident,body(detail,True)).json()['company']
    legacy=dict(version=switched['version'],name='Legacy rename',active=True)
    key=str(uuid4());result=save(client,ident,legacy,key)
    assert result.status_code==200 and result.json()['company']['tms_workspace'] is True
    assert save(client,ident,legacy,key).json()==result.json()
    assert save(client,ident,{**legacy,'tms_workspace':False},key).status_code==409
    payload=company_payload(CompanyOverviewUpdate.model_validate(legacy))
    assert payload=={**legacy,'dispatcher':None}
    assert company_payload(CompanyOverviewUpdate.model_validate({**legacy,'tms_workspace':False}))=={**payload,'tms_workspace':False}


def test_failed_native_rate_restore_leaves_mode_and_company_unchanged(client):
    ident,detail=setup_company(client)
    detail=save(client,ident,body(detail,True)).json()['company']
    with owner_engine.begin() as db:
        db.execute(text("UPDATE rate_cards SET active=false WHERE organization_id=:id AND data->>'method'<>'IMPORTED'"),{'id':ident})
    result=save(client,ident,{**body(detail,False),'name':'Not saved'})
    assert result.status_code==409,result.text
    after=client.get(f'{ROOT}/{ident}').json()
    from datetime import datetime
    assert datetime.fromisoformat(after.pop('updated_at'))==datetime.fromisoformat(detail.pop('updated_at'))
    assert after==detail
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT data->>'method' FROM rate_cards WHERE organization_id=:id AND is_default"),{'id':ident}).scalar_one()=='IMPORTED'
