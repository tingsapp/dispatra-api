"""Truck transport boundary and restricted PostgreSQL integration; no real provider calls."""
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import now
from app.operations import tomtom, travel
from app.operations.models import Order, Route, Catalog
from app.order_watch import traffic, estimates
from app.order_watch.models import RouteEstimate
from app.assistant import provider
from app.assistant.schemas import SearchPlan, GroundedAnswer
from conftest import post, login, company, owner_engine
from test_manual_operations import setup as seeded_setup, new_order, assign, check, BASE, address


@pytest.fixture
def setup(seeded_setup):
    """Keep pre-profile fleet compatibility coverage independent of current seed defaults."""
    with Session(owner_engine) as db, db.begin():
        for row in db.scalars(select(Catalog).where(Catalog.kind == 'VEHICLE_TYPE')):
            row.data = {k: v for k, v in row.data.items() if k != 'road_profile'}
    seeded_setup['catalog'] = check(seeded_setup['client'].get(BASE+'/catalog'))
    return seeded_setup


PROFILE = dict(length_m='8.2', width_m='2.5', height_m='3.8', gross_weight_kg=12000, axle_weight_kg=6500)
VEHICLE = dict(road_profile=PROFILE)


def reply(legs=1):
    return {'routes': [{'legs': [dict(summary=dict(travelTimeInSeconds=600, trafficDelayInSeconds=120,
        lengthInMeters=2400), points=[dict(latitude=49.26+i*.01, longitude=-123.11),
        dict(latitude=49.27+i*.01, longitude=-123.11)]) for i in range(legs)],
        'sections': [{'sectionType': 'TRAVEL_MODE', 'travelMode': 'truck'}]}]}


def test_truck_request_has_explicit_dimensions_traffic_no_contact_or_car_defaults(monkeypatch):
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def read(self,_): return json.dumps(reply()).encode()
    monkeypatch.setenv('TOMTOM_API_KEY','test-secret')
    monkeypatch.setattr(tomtom.urllib.request,'urlopen',lambda req,timeout: calls.append(req) or Response())
    result=tomtom.route([address(),address(lat=49.27)],VEHICLE,geometry=True)
    query=parse_qs(urlparse(calls[0].full_url).query)
    assert query['travelMode']==['truck'] and query['traffic']==['true'] and query['vehicleCommercial']==['true']
    assert query['vehicleHeight']==['3.8'] and query['vehicleWeight']==['12000']
    assert 'Main Street' not in calls[0].full_url and 'phone' not in calls[0].full_url
    assert result['points']==[[49.26,-123.11],[49.27,-123.11]] and result['legs'][0]['delay_seconds']==120


@pytest.mark.parametrize('vehicle,hazardous',[(None,False),({'height_cm':380,'payload_kg':12000},False),
    ({'road_profile':{**PROFILE,'height_m':'NaN'}},False),(VEHICLE,True)])
def test_missing_or_invalid_exterior_profile_and_unclassified_dg_block_before_provider(monkeypatch,vehicle,hazardous):
    monkeypatch.setattr(tomtom,'request',lambda *_: pytest.fail('must not call provider'))
    with pytest.raises(HTTPException) as error: tomtom.route([address(),address()],vehicle,hazardous=hazardous)
    assert error.value.status_code==409


def test_missing_key_and_remote_errors_do_not_leak_urls_or_credentials(monkeypatch):
    monkeypatch.delenv('TOMTOM_API_KEY',raising=False)
    with pytest.raises(HTTPException) as error: tomtom.request('/routing/test',{})
    assert error.value.status_code==409
    monkeypatch.setenv('TOMTOM_API_KEY','private-test-key')
    def fail(*_,**__): raise OSError('https://provider?key=private-test-key')
    monkeypatch.setattr(tomtom.urllib.request,'urlopen',fail)
    with pytest.raises(HTTPException) as error: tomtom.request('/routing/test',{})
    assert error.value.status_code==503 and 'private-test-key' not in error.value.detail


@pytest.mark.parametrize('bad',[{'routes':[]}, {'routes':[{'legs':[]}]},
    {'routes':[{'legs':[{'summary':{'travelTimeInSeconds':float('nan')}}]}]},
    {**reply(),'routes':[{**reply()['routes'][0],'sections':[{'sectionType':'TRAVEL_MODE','travelMode':'other'}]}]}])
def test_incomplete_or_non_truck_results_cannot_become_a_valid_eta(monkeypatch,bad):
    monkeypatch.setattr(tomtom,'request',lambda *_: bad)
    with pytest.raises(HTTPException) as error: tomtom.route([address(),address()],VEHICLE)
    assert error.value.status_code==503


def test_matrix_chunks_preserves_unreachable_cells_and_separates_vehicle_profiles(monkeypatch):
    calls=[]
    def matrix_reply(path,params,body):
        calls.append(body)
        rows=[]
        for i in range(len(body['origins'])):
            for j in range(len(body['destinations'])):
                rows.append(dict(originIndex=i,destinationIndex=j,routeSummary=dict(travelTimeInSeconds=120)))
        return {'data':rows}
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    monkeypatch.setattr(tomtom,'request',matrix_reply)
    stops=[{'id':str(i),'address':address(lat=49.26+i*.001)} for i in range(11)]
    db=SimpleNamespace(info={})
    table=travel.matrix(db,None,stops,vehicle=VEHICLE)
    assert len(table)==121 and len(calls)==4
    assert all(len(c['origins'])*len(c['destinations'])<=100 for c in calls)
    assert travel.matrix(db,None,stops,vehicle=VEHICLE)==table and len(calls)==4
    travel.matrix(db,None,stops,vehicle={'road_profile':{**PROFILE,'height_m':4.2}})
    assert len(calls)==8 and calls[-1]['options']['vehicleHeight']==4.2
    def unreachable(*_): return {'data':[dict(originIndex=0,destinationIndex=0,detailedError={
        'code':'CELL_PROCESSING_ERROR','innerError':{'code':'NO_ROUTE_FOUND'}})]}
    monkeypatch.setattr(tomtom,'request',unreachable)
    assert travel.matrix(None,None,stops[:1],vehicle=VEHICLE)=={}
    with pytest.raises(HTTPException): travel.matrix(None,None,stops,vehicle=VEHICLE)
    with pytest.raises(HTTPException) as error: travel.matrix(None,None,stops*5,vehicle=VEHICLE)
    assert error.value.status_code==409


def test_live_truck_estimate_accumulates_traffic_service_and_never_claims_certified_access(monkeypatch):
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    monkeypatch.setattr(tomtom,'request',lambda *_: reply(2))
    current=now()
    data=traffic.obtain(None,dict(origin=address(),vehicle=VEHICLE,targets=[
        dict(stop_id='a',address=address(),service_minutes=30),
        dict(stop_id='b',address=address(),service_minutes=0)]),current)
    assert data['stops'][1]['eta']==(current+timedelta(minutes=50)).isoformat()
    assert data['stops'][1]['traffic_added_seconds']==240 and data['truck_profile_applied'] is True
    assert data['vehicle_access_verified'] is False


def test_vehicle_profile_update_preserves_older_clients_retry_shape_and_tenant_scope(setup):
    w=setup;client=w['client'];vehicle=w['vehicle']
    payload={**vehicle['data'],'road_profile':PROFILE}
    def save(data,version,key=None):
        return client.put(BASE+f'/vehicles/{vehicle["id"]}',json=dict(data=data,version=version),headers={'Idempotency-Key':key or str(uuid4())})
    updated=check(save(payload,vehicle['version']))
    legacy={k:v for k,v in updated['data'].items() if k!='road_profile'}
    key=str(uuid4());preserved=check(save(legacy,updated['version'],key))
    assert preserved['data']['road_profile']==PROFILE
    assert check(save(legacy,updated['version'],key))==preserved
    assert save(legacy,updated['version']).status_code==409
    bad={**preserved['data'],'road_profile':{**PROFILE,'gross_weight_kg':-1}}
    assert save(bad,preserved['version']).status_code==422
    assert save({**legacy,'payload_kg':'13000'},preserved['version']).status_code==422
    login(client);company(client,'other');login(client,'dispatch','other','dispatcher')
    assert client.put(BASE.replace('acme','other')+f'/vehicles/{vehicle["id"]}',json=dict(data=legacy,version=preserved['version']),headers={'Idempotency-Key':str(uuid4())}).status_code==404


def test_dispatch_assignment_and_map_use_tomtom_without_repricing_and_keep_role_boundaries(setup,monkeypatch):
    w=setup;client=w['client'];vehicle=w['vehicle']
    w['vehicle']=check(client.put(BASE+f'/vehicles/{vehicle["id"]}',json=dict(version=vehicle['version'],
        data={**vehicle['data'],'road_profile':PROFILE}),headers={'Idempotency-Key':str(uuid4())}))
    order=new_order(w)
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    calls=[]
    def response(path,params,payload=None):
        calls.append((path,params,payload))
        if payload:
            return {'data':[dict(originIndex=i,destinationIndex=j,routeSummary={'travelTimeInSeconds':60})
                for i in range(len(payload['origins'])) for j in range(len(payload['destinations']))]}
        return reply()
    monkeypatch.setattr(tomtom,'request',response)
    route=assign(w,order)
    assert route['plan']['travel_basis']=='TOMTOM_TRUCK_TRAFFIC'
    assert check(client.get(BASE+f'/orders/{order["id"]}'))['pricing']==order['pricing']
    path=check(client.get(BASE+f'/orders/{order["id"]}/road-path'))
    assert path['provider']=='TOMTOM' and path['truck_profile_applied'] and path['attribution']=='© TomTom' and not path['preview']
    count=len(calls)
    assert w['mobile'].get(BASE+f'/orders/{order["id"]}/road-path').status_code==403
    login(client);company(client,'other');login(client,'dispatch','other','dispatcher')
    assert client.get(BASE.replace('acme','other')+f'/orders/{order["id"]}/road-path').status_code==404
    assert len(calls)==count


def test_monitor_and_assistant_show_only_fresh_same_route_estimates(setup,monkeypatch):
    w=setup;client=w['client'];order=new_order(w);route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    current=now();first=route['stops'][0]['stop_id']
    with Session(owner_engine) as db,db.begin():
        saved=db.get(Route,UUID(route['id']))
        estimate=RouteEstimate(organization_id=saved.organization_id,route_id=saved.id,generation=saved.generation,
            route_version=saved.version,next_stop_id=UUID(first),captured_at=current,observed_at=current,
            data=dict(source='TomTom truck route with traffic',truck_profile_applied=True,duty_id=w['duty']['id'],
                stops=[dict(stop_id=first,eta=(current+timedelta(minutes=20)).isoformat(),traffic_added_seconds=180,road_distance_metres=2000)]))
        db.add(estimate)
    view=check(client.get(BASE+'/monitor'))
    estimate=view['routes'][0]['estimate'];assert estimate['truck_profile_applied'] and estimate['stops'][0]['traffic_added_seconds']==180
    assert 'duty_id' not in estimate
    facts=[]
    def answer(schema,instructions,messages):
        if schema is SearchPlan: return SearchPlan(include_records=True,query=order['number'],kinds=['ORDER'],order_statuses=[])
        facts.append(messages[-1]['content']);return GroundedAnswer(message='Estimated arrival from recorded facts.',references=[])
    monkeypatch.setattr(provider,'generate',answer)
    check(client.post(BASE+'/assistant/messages',json={'message':f'ETA for {order["number"]}'}))
    assert 'TomTom truck route with traffic' in facts[0] and 'traffic delay 3.0 min' in facts[0]
    def driver_answer(schema,instructions,messages):
        if schema is SearchPlan: return SearchPlan(include_records=True,query='',kinds=['DRIVER'],order_statuses=[])
        facts.append(messages[-1]['content']);return GroundedAnswer(message='Driver arrival estimate from recorded facts.',references=[])
    monkeypatch.setattr(provider,'generate',driver_answer)
    check(client.post(BASE+'/assistant/messages',json={'message':'What is the driver arrival time?'}))
    assert 'TomTom truck route with traffic' in facts[-1] and 'traffic delay 3.0 min' in facts[-1]

    with Session(owner_engine) as db,db.begin():
        stored=db.scalar(select(RouteEstimate));stored.captured_at=current-timedelta(minutes=3)
    assert check(client.get(BASE+'/monitor'))['routes'][0]['estimate'] is None
    assert w['mobile'].get(BASE+'/monitor').status_code==403
    login(client);company(client,'other');login(client,'dispatch','other','dispatcher')
    assert check(client.get(BASE.replace('acme','other')+'/monitor'))['routes']==[]


def test_past_booking_departure_uses_current_traffic_not_rejected_past_time(monkeypatch):
    captured=[]
    monkeypatch.setattr(tomtom,'request',lambda path,params: captured.append(params) or reply())
    tomtom.route([address(),address()],VEHICLE,departure=now()-timedelta(days=1))
    assert captured[-1]['departAt']=='now'
    future=now()+timedelta(days=1)
    tomtom.route([address(),address()],VEHICLE,departure=future)
    assert captured[-1]['departAt']==future.isoformat()


def test_unassigned_monitor_preview_survives_tomtom_activation_without_truck_facts(setup, monkeypatch):
    w=setup;client=w['client'];order=new_order(w)
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    before=check(client.get(BASE+f'/orders/{order["id"]}'))
    calls=[]
    monkeypatch.setattr(travel,'order_road_path',lambda db,actor,booking:
        calls.append(booking) or [[49.26,-123.11],[49.27,-123.11]])
    monkeypatch.setattr(tomtom,'request',lambda *_: pytest.fail('unassigned preview is not a truck route'))
    path=check(client.get(BASE+f'/orders/{order["id"]}/road-path'))
    assert path==dict(points=[[49.26,-123.11],[49.27,-123.11]],provider='GOOGLE',
        truck_profile_applied=False,attribution='',preview=True)
    assert [str(s.id) for s in calls[0].stops]==[s['id'] for s in order['facts']['stops']]
    assert check(client.get(BASE+f'/orders/{order["id"]}'))==before
    assert w['mobile'].get(BASE+f'/orders/{order["id"]}/road-path').status_code==403
    login(client);company(client,'other');login(client,'dispatch','other','dispatcher')
    assert client.get(BASE.replace('acme','other')+f'/orders/{order["id"]}/road-path').status_code==404
    assert len(calls)==1


def test_assignment_switches_preview_to_truck_and_missing_profile_never_falls_back(setup, monkeypatch):
    w=setup;client=w['client'];order=new_order(w);assign(w,order)
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    monkeypatch.setattr(travel,'order_road_path',lambda *_: pytest.fail('assigned route cannot use preview'))
    monkeypatch.setattr(tomtom,'request',lambda *_: pytest.fail('missing profile blocks before transport'))
    blocked=client.get(BASE+f'/orders/{order["id"]}/road-path')
    assert blocked.status_code==409
    assert 'truck' in blocked.text.lower()


def test_preview_provider_failure_stays_explicit_without_straight_line_or_truck_fallback(setup, monkeypatch):
    w=setup;client=w['client'];order=new_order(w)
    monkeypatch.setenv('DISPATCH_ROUTING_PROVIDER','tomtom')
    def unavailable(*_):raise HTTPException(503,'Road routing provider is unavailable.')
    monkeypatch.setattr(travel,'order_road_path',unavailable)
    monkeypatch.setattr(tomtom,'request',lambda *_: pytest.fail('no alternate provider fallback'))
    assert client.get(BASE+f'/orders/{order["id"]}/road-path').status_code==503
