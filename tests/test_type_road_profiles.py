"""Shared Vehicle Type truck facts: real restricted PostgreSQL, no external calls."""
import pytest
from uuid import UUID,uuid4
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import now
from app.operations import tomtom
from app.operations.models import Vehicle,Route,Catalog,Location
from app.operations.road_profiles import vehicle_data
from app.order_watch.traffic import route_input
from test_tomtom import PROFILE,reply,setup,seeded_setup
from test_manual_operations import new_order,assign,check,BASE
from conftest import post,login,company,owner_engine


def kind(w):return next(c for c in check(w['client'].get(BASE+'/catalog')) if c['id']==w['type_id'])
def save_kind(w,data,version,key=None):return w['client'].put(BASE+f'/catalog/{w["type_id"]}',json=dict(data=data,version=version),headers={'Idempotency-Key':key or str(uuid4())})


def test_type_profile_roundtrip_legacy_retry_validation_and_tenant_boundaries(setup):
 w=setup;c=kind(w)
 saved=check(save_kind(w,{**c['data'],'road_profile':PROFILE},c['version']))
 assert saved['data']['road_profile']==PROFILE
 # Older clients omit the new field; saved values and retry hashes are preserved.
 legacy={k:v for k,v in saved['data'].items() if k!='road_profile'};key=str(uuid4())
 kept=check(save_kind(w,legacy,saved['version'],key));assert kept['data']['road_profile']==PROFILE
 assert check(save_kind(w,legacy,saved['version'],key))==kept
 assert save_kind(w,legacy,saved['version']).status_code==409
 for profile in [{**PROFILE,'height_m':'NaN'},{**PROFILE,'gross_weight_kg':500},{**PROFILE,'axle_weight_kg':15000}]:
  assert save_kind(w,{**kept['data'],'road_profile':profile},kept['version']).status_code==422
 assert save_kind(w,{**legacy,'payload_kg':'13000'},kept['version']).status_code==422
 login(w['client']);company(w['client'],'other');login(w['client'],'dispatch','other','dispatcher')
 assert w['client'].put(BASE.replace('acme','other')+f'/catalog/{w["type_id"]}',json=dict(data=legacy,version=kept['version']),headers={'Idempotency-Key':str(uuid4())}).status_code==404


def test_shared_type_profile_reaches_assignment_map_and_live_traffic_without_vehicle_fields(setup,monkeypatch):
 w=setup;c=kind(w);check(save_kind(w,{**c['data'],'road_profile':PROFILE},c['version']))
 assert w['vehicle']['data']['road_profile'] is None
 seen=[]
 def request(path,params,payload=None):
  seen.append((path,params,payload))
  if payload:return {'data':[dict(originIndex=i,destinationIndex=j,routeSummary=dict(travelTimeInSeconds=60)) for i in range(len(payload['origins'])) for j in range(len(payload['destinations']))]}
  return reply(path.count(':'))
 monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom');monkeypatch.setattr(tomtom,'request',request)
 order=new_order(w);price=order['pricing'];route=assign(w,order)
 assert seen[0][2]['options']['vehicleHeight']==3.8
 assert check(w['client'].get(BASE+f'/orders/{order["id"]}'))['pricing']==price
 assert check(w['client'].get(BASE+f'/orders/{order["id"]}/road-path'))['truck_profile_applied']
 assert seen[-1][1]['vehicleWeight']==12000
 with Session(owner_engine) as db,db.begin():
  vehicle=db.get(Vehicle,UUID(w['vehicle']['id']))
  assert vehicle_data(db,vehicle.organization_id,vehicle)['road_profile']==PROFILE
  # A legacy per-vehicle value cannot override a configured type.
  vehicle.data={**vehicle.data,'road_profile':{**PROFILE,'height_m':'2.0'}}
  assert vehicle_data(db,vehicle.organization_id,vehicle)['road_profile']['height_m']=='3.8'
 route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
 with Session(owner_engine) as db,db.begin():
  saved=db.get(Route,UUID(route['id']));current=now()
  db.add(Location(organization_id=saved.organization_id,driver_id=saved.driver_id,duty_id=UUID(w['duty']['id']),captured_at=current,data=dict(latitude=49.26,longitude=-123.11,accuracy_m=5)))
  db.flush()
  from types import SimpleNamespace
  snapshot=route_input(db,SimpleNamespace(organization_id=saved.organization_id),saved,current)
  assert snapshot['vehicle']['road_profile']==PROFILE


def test_type_profile_changes_cannot_mutate_active_routes_and_legacy_values_remain_readable(setup):
 w=setup
 vehicle=w['vehicle'];saved=check(w['client'].put(BASE+f'/vehicles/{vehicle["id"]}',json=dict(version=vehicle['version'],data={**vehicle['data'],'road_profile':PROFILE}),headers={'Idempotency-Key':str(uuid4())}))
 with Session(owner_engine) as db:
  row=db.get(Vehicle,UUID(vehicle['id']));assert vehicle_data(db,row.organization_id,row)['road_profile']==PROFILE
 order=new_order(w);assign(w,order);c=kind(w)
 assert save_kind(w,{**c['data'],'road_profile':PROFILE},c['version']).status_code==409
 # Omitted fields on an older catalogue client do not change the effective routing profile.
 legacy={k:v for k,v in c['data'].items() if k!='road_profile'}
 assert save_kind(w,{**legacy,'name':'Renamed truck type'},c['version']).status_code==422
 assert w['mobile'].put(BASE+f'/catalog/{w["type_id"]}',json=dict(version=c['version'],data=c['data']),headers={'Idempotency-Key':str(uuid4())}).status_code==403


def test_type_clear_and_type_change_do_not_reuse_wrong_vehicle_measurements(setup):
 w=setup;c=kind(w);saved=check(save_kind(w,{**c['data'],'road_profile':PROFILE},c['version']))
 assert save_kind(w,{**saved['data'],'road_profile':None},saved['version']).status_code==422
 with Session(owner_engine) as db,db.begin():
  row=db.get(Vehicle,UUID(w['vehicle']['id']));row.data={**row.data,'road_profile':PROFILE}
  assert vehicle_data(db,row.organization_id,row)['road_profile']==PROFILE
 other=next(c for c in w['catalog'] if c['kind']=='VEHICLE_TYPE' and c['id']!=w['type_id'])
 v=check(w['client'].get(BASE+f'/vehicles/{w["vehicle"]["id"]}'));data={k:val for k,val in v['data'].items() if k!='road_profile'};data['type_id']=other['id']
 changed=check(w['client'].put(BASE+f'/vehicles/{v["id"]}',json=dict(data=data,version=v['version']),headers={'Idempotency-Key':str(uuid4())}))
 assert changed['data']['road_profile'] is None


def test_existing_missing_profiles_stay_readable_but_must_be_completed_before_type_save(setup):
 w=setup;order=new_order(w);route=assign(w,order);c=kind(w)
 assert c['data']['road_profile'] is None
 assert save_kind(w,{**c['data'],'name':'More specific configuration','road_profile':None},c['version']).status_code==422
 assert kind(w)['data']['name']==c['data']['name']
 assert next(r for r in check(w['client'].get(BASE+'/routes')) if r['id']==route['id'])['version']==route['version']


def test_new_vehicle_types_require_profiles_but_other_catalogues_do_not(setup):
 w=setup;c=kind(w)
 data={k:v for k,v in c['data'].items() if k!='road_profile'}
 for profile in ['omitted',None]:
  payload=data if profile=='omitted' else {**data,'road_profile':None}
  assert post(w['client'],BASE+'/catalog',dict(kind='VEHICLE_TYPE',code='NEW_TYPE',data=payload)).status_code==422
 created=check(post(w['client'],BASE+'/catalog',dict(kind='VEHICLE_TYPE',code='NEW_TYPE',data={**data,'road_profile':PROFILE})),201)
 assert created['data']['road_profile']==PROFILE
 service=check(post(w['client'],BASE+'/catalog',dict(kind='SERVICE',code='NEW_SERVICE',data=dict(name='New service'))),201)
 assert service['data']['road_profile'] is None


@pytest.mark.parametrize('started', [False, True])
def test_first_type_profile_setup_preserves_google_trip_and_still_locks_later_changes(setup, monkeypatch, started):
 w=setup;order=new_order(w);route=assign(w,order)
 if started:
  route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
 before_route=next(r for r in check(w['client'].get(BASE+'/routes')) if r['id']==route['id'])
 before_order=check(w['client'].get(BASE+f'/orders/{order["id"]}'))
 c=kind(w);key=str(uuid4());payload={**c['data'],'road_profile':PROFILE}
 # A deployed truck router cannot have its effective restrictions changed under an active trip.
 monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
 assert save_kind(w,payload,c['version']).status_code==409
 monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','google')
 saved=check(save_kind(w,payload,c['version'],key))
 assert saved['data']['road_profile']==PROFILE
 assert check(save_kind(w,payload,c['version'],key))==saved
 assert next(r for r in check(w['client'].get(BASE+'/routes')) if r['id']==route['id'])==before_route
 assert check(w['client'].get(BASE+f'/orders/{order["id"]}'))==before_order
 assert save_kind(w,{**saved['data'],'road_profile':{**PROFILE,'height_m':'4.2'}},saved['version']).status_code==409
 assert save_kind(w,{**saved['data'],'road_profile':None},saved['version']).status_code==422
 assert next(r for r in check(w['client'].get(BASE+'/routes')) if r['id']==route['id'])==before_route
