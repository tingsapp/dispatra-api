"""Owner plan projections come from each company's saved workspace mode."""
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.orm import Session
from conftest import PASSWORD, company, login, owner_engine, post
from app.database import context, engine
from app.platform.queries import include_company_details

PATH='/api/v1/platform/organizations'


def test_owner_list_detail_and_updates_show_each_saved_plan_and_matching_creation_price(client):
    login(client)
    terms=client.get('/api/v1/platform/billing/plan').json()
    result=client.patch('/api/v1/platform/billing/plan',json=dict(version=terms['version'],monthly_price='99.95',currency='CAD',dispatch_monthly_price='29.90',dispatch_currency='USD'),headers={'Idempotency-Key':str(uuid4())})
    assert result.status_code==200,result.text
    companies=[]
    for external in [False,True]:
        suffix='dispatch' if external else 'full'
        made=post(client,PATH,dict(name=f'Plan {suffix}',slug=f'plan-{suffix}',admin_login=f'{suffix}@example.com',password=PASSWORD,tms_workspace=external))
        assert made.status_code==201,made.text
        ident=made.json()['id'];companies.append((ident,external))
        detail=client.get(f'{PATH}/{ident}').json()
        assert detail['tms_workspace'] is external
        agreement=client.get(f'{PATH}/{ident}/subscription').json()
        assert (agreement['monthly_price'],agreement['currency']) == (('29.90','USD') if external else ('99.95','CAD'))
        updated=client.patch(f'{PATH}/{ident}',json=dict(version=detail['version'],name=f'Updated {suffix}'),headers={'Idempotency-Key':str(uuid4())})
        assert updated.status_code==200,updated.text
        assert updated.json()['tms_workspace'] is external
    listing={row['id']:row for row in client.get(PATH).json()}
    for ident,external in companies: assert listing[ident]['tms_workspace'] is external
    ident,external=companies[1]
    old=client.get(f'{PATH}/{ident}').json();old.pop('tms_workspace')
    with Session(engine) as db,db.begin():
        context(db,platform=True)
        assert include_company_details(db,ident,old)['tms_workspace'] is True
    login(client,'dispatch',login_id='dispatch@example.com')
    assert client.get(PATH).status_code==403
    assert client.get(f'{PATH}/{companies[0][0]}').status_code==403


def test_plan_projection_defaults_legacy_or_missing_settings_to_full_without_truthy_string_coercion(client):
    login(client);org=company(client);ident=org['id']
    for value in [None,'true',False,True]:
        with owner_engine.begin() as db:
            if value is None:db.execute(text("UPDATE company_settings SET data=data-'tms_workspace' WHERE organization_id=:id"),{'id':ident})
            else:
                import json
                db.execute(text("UPDATE company_settings SET data=jsonb_set(data,'{tms_workspace}',CAST(:value AS jsonb)) WHERE organization_id=:id"),{'id':ident,'value':json.dumps(value)})
        expected=value is True
        assert client.get(f'{PATH}/{ident}').json()['tms_workspace'] is expected
        assert client.get(PATH).json()[0]['tms_workspace'] is expected
    with owner_engine.begin() as db:
        db.execute(text('DELETE FROM company_settings WHERE organization_id=:id'),{'id':ident})
    assert client.get(PATH).json()[0]['tms_workspace'] is False
    assert client.get(f'{PATH}/{ident}').json()['tms_workspace'] is False


def test_owner_projection_reflects_dispatcher_mode_changes_without_changing_other_companies(client):
    login(client);first=company(client);second=company(client,'other')
    login(client,'dispatch','acme','dispatcher')
    path='/api/v1/companies/acme/settings';saved=client.get(path).json()
    changed=client.put(path,json={'version':saved['version'],'data':{**saved['data'],'tms_workspace':True}},headers={'Idempotency-Key':str(uuid4())})
    assert changed.status_code==200,changed.text
    login(client)
    rows={row['id']:row for row in client.get(PATH).json()}
    assert rows[first['id']]['tms_workspace'] is True
    assert rows[second['id']]['tms_workspace'] is False
    assert client.get(f"{PATH}/{first['id']}").json()['tms_workspace'] is True
