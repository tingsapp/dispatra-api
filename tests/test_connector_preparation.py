import json
import base64
from urllib.parse import parse_qs
import pytest
from app.connectors import prepared, profiles, transport
from app.integrations import client as http


def contract(provider):
    return {'provider': provider, 'revision':'acceptance-fixture', 'api_version':'fixture',
            'evidence':'Synthetic completeness check; not vendor acceptance',
            'account_id_path':'organization.id','account_label_path':'organization.name',
            'resources':{'order':{'path':'/orders'}},
            'maps':{'order':{'id':'id','scheduled_at':'date','service':'service','total':'total','tax':'tax',
                            'shipper.id':'customer.id','shipper.name':'customer.name','shipper.email':'customer.email',
                            'stops':'stops','stop.id':'id','stop.type':'kind','stop.pickup_value':'P','stop.delivery_value':'D',
                            'stop.address':'address','stop.city':'city','stop.province':'province','stop.postal_code':'postal',
                            'items':'items','item.weight':'weight','item.length':'length','item.width':'width','item.height':'height',
                            'item.pickup_stop':'pickup','item.delivery_stop':'delivery'}},
            'weight_unit':'kg','dimension_unit':'cm','currency':'CAD'}


def test_rose_rocket_defaults_prepare_registered_oauth_without_invented_mapping():
    value=profiles.Profile.model_validate(prepared.prepare('rose-rocket',contract('rose-rocket')))
    assert value.authorize_url=='https://a.roserocket.com/authorize'
    assert value.token_url=='https://a.roserocket.com/oauth/token'
    assert value.identity.path=='/api/v1/me'
    assert value.scopes=='offline_access email profile'
    assert value.audience=='https://roserocket.com'
    with pytest.raises(ValueError):
        profiles.Profile.model_validate(prepared.prepare('rose-rocket',{'provider':'rose-rocket'}))


def test_cartoncloud_requires_registered_vendor_url_and_complete_mapping():
    supplied=contract('cartoncloud')
    with pytest.raises(ValueError,match='Registered OAuth application'):
        profiles.Profile.model_validate(prepared.prepare('cartoncloud',supplied))
    value=profiles.Profile.model_validate(prepared.prepare('cartoncloud',{**supplied,'authorize_url':'https://login.cartoncloud.com/vendor-approved'}))
    assert value.headers=={'Accept-Version':'8'}
    assert value.token_auth=='basic'
    assert value.identity.path=='/users/me'
    assert value.identity_scope=='cartoncloud_tenant'


def test_cartoncloud_token_uses_basic_auth_and_version_header(monkeypatch):
    value=profiles.Profile.model_validate(prepared.prepare('cartoncloud',{**contract('cartoncloud'),'authorize_url':'https://login.cartoncloud.com/vendor-approved'}))
    monkeypatch.setenv('CARTONCLOUD_CLIENT_ID','sample-id')
    monkeypatch.setenv('CARTONCLOUD_CLIENT_SECRET','sample-secret')
    monkeypatch.setattr(http,'check_host',lambda url:None)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b'{"access_token":"fixture-token"}'
    def opened(req,timeout):
        assert req.full_url=='https://api.cartoncloud.com/uaa/oauth/token'
        assert req.get_header('Accept-version')=='8'
        assert req.get_header('Authorization')=='Basic '+base64.b64encode(b'sample-id:sample-secret').decode()
        body=parse_qs(req.data.decode())
        assert body['grant_type']==['authorization_code']
        assert 'client_secret' not in body
        return Response()
    monkeypatch.setattr(http._opener,'open',opened)
    assert transport.token(value,{'grant_type':'authorization_code','code':'fixture-code'})['access_token']=='fixture-token'


def test_cartoncloud_identity_uses_company_not_user_and_refuses_ambiguous_access(monkeypatch):
    value=profiles.Profile.model_validate(prepared.prepare('cartoncloud',{**contract('cartoncloud'),'authorize_url':'https://login.cartoncloud.com/vendor-approved'}))
    tenants=[{'id':'company-one','name':'First Company'}]
    monkeypatch.setattr(http,'request',lambda *args,**kwargs:{'id':'user-id','name':'Personal User','tenants':tenants})
    assert transport.identity('cartoncloud',value,{'access_token':'fixture'})==('company-one','First Company')
    tenants.append({'id':'company-two','name':'Second Company'})
    with pytest.raises(transport.Error,match='ACCOUNT_SELECTION_REQUIRED'):
        transport.identity('cartoncloud',value,{'access_token':'fixture'})
    assert transport.identity('cartoncloud',value,{'access_token':'fixture','account_id':'company-one'})[0]=='company-one'
    with pytest.raises(transport.Error,match='ACCOUNT_CHANGED'):
        transport.identity('cartoncloud',value,{'access_token':'fixture','account_id':'unknown-company'})
    resource=profiles.Resource(path='/tenants/{account_id}/consignments/search',method='POST')
    assert '/tenants/company-one/' in transport.target(value,resource,{'account_id':'company-one'})
    with pytest.raises(transport.Error,match='IDENTITY_NOT_VERIFIED'):
        transport.target(value,resource,{})


def test_pending_providers_cannot_be_activated_by_installing_a_profile(monkeypatch,tmp_path):
    path=tmp_path/'profiles.json'
    path.write_text(json.dumps({name:contract(name) for name in ['truckbase','tailwind','ascendtms']}))
    monkeypatch.setenv('TMS_CONNECTOR_PROFILES_FILE',str(path))
    for name in ['truckbase','tailwind','ascendtms']:
        assert profiles.installed(name) is None


def test_prepared_profile_still_requires_registered_credentials(monkeypatch,tmp_path):
    path=tmp_path/'profiles.json';path.write_text(json.dumps({'rose-rocket':contract('rose-rocket')}))
    monkeypatch.setenv('TMS_CONNECTOR_PROFILES_FILE',str(path))
    monkeypatch.delenv('ROSE_ROCKET_CLIENT_ID',raising=False)
    monkeypatch.delenv('ROSE_ROCKET_CLIENT_SECRET',raising=False)
    assert profiles.installed('rose-rocket') is None
    monkeypatch.setenv('ROSE_ROCKET_CLIENT_ID','fixture-id')
    monkeypatch.setenv('ROSE_ROCKET_CLIENT_SECRET','fixture-secret')
    assert profiles.installed('rose-rocket') is not None
