"""Geographic fleet planning and live route insertion through restricted-role HTTP commands."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.models import now
from app.dispatch import service
from app.dispatch.geography import movement, zone
from app.operations.schemas import SettingsData
from app.operations.routing import optimize
from conftest import HEADERS, login, company, post, owner_engine
from test_manual_operations import BASE, check, address, booking, new_order, assign, setup
from test_dispatch_agent import fleet, mode, suggest


def gps(w, **updates):
    body=dict(duty_id=w['duty']['id'],captured_at=now().isoformat(),latitude=49.26,longitude=-123.11,
        accuracy_m=10,location_permission='GRANTED')
    body.update(updates)
    return check(post(w['mobile'],BASE+'/driver/location',body),201)


def active(w):
    order=new_order(w)
    route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'],supports_live_updates=True)))
    gps(w)
    return order,route


def approve(w, decision, key=None):
    return post(w['client'],BASE+f'/dispatch-decisions/{decision["id"]}/approve',dict(version=decision['version'],driver_id=w['driver']['id']),key)


def arrive(w, route, visit):
    return check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=now().isoformat())))


def complete_pickup(w,route):
    visit=route['stops'][0]
    route=arrive(w,route,visit)
    return check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/complete',dict(version=route['version'],generation=route['generation'],
        captured_at=now().isoformat(),quantities={i['id']:i['quantity'] for i in route['items'] if i['pickup_id']==visit['stop_id']})))


def test_live_insert_preserves_custody_visits_prices_and_requires_review(fleet):
    w=fleet;first,route=active(w)
    route=complete_pickup(w,route)
    completed=route['stops'][0]
    next_id=route['stops'][1]['stop_id']
    second=new_order(w)
    decision=check(suggest(w['client'],second))
    assert decision['candidates'][0]['metrics']['live_insertion']
    key=str(uuid4())
    result=check(approve(w,decision,key));updated=result['route']
    assert check(approve(w,decision,key))==result
    assert updated['id']==route['id'] and updated['generation']==route['generation']
    assert updated['plan']['requires_acknowledgement'] and updated['plan']['revision']==2
    assert updated['stops'][0]['id']==completed['id']
    assert updated['stops'][0]['movements']==completed['movements']
    assert datetime.fromisoformat(updated['stops'][0]['completed_at'])==datetime.fromisoformat(completed['completed_at'])
    assert updated['stops'][1]['stop_id']==next_id
    assert len(updated['stops'])==4
    for order in (first,second):
        assert check(w['client'].get(BASE+f'/orders/{order["id"]}'))['pricing']==order['pricing']
    mine=check(w['mobile'].get(BASE+f'/driver/routes/{route["id"]}'))
    assert 'pricing' not in str(mine) and mine['plan']['requires_acknowledgement']
    visit=mine['stops'][1]
    rejected=post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',dict(version=mine['version'],generation=mine['generation'],captured_at=now().isoformat()))
    assert rejected.status_code==409 and 'acknowledge' in rejected.text
    ackkey=str(uuid4());body=dict(version=mine['version'],generation=mine['generation'])
    acknowledged=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/acknowledge',body,ackkey))
    assert not acknowledged['plan']['requires_acknowledgement']
    assert check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/acknowledge',body,ackkey))==acknowledged
    assert arrive(w,acknowledged,visit)['stops'][1]['arrived_at']
    assert any(n['title']=='Route updated' for n in check(w['mobile'].get(BASE+'/driver/notifications')))


@pytest.mark.parametrize('kind',['stale','inaccurate','missing'])
def test_live_insert_refuses_unreliable_gps(fleet,kind):
    w=fleet;_,route=active(w)
    with owner_engine.begin() as db:
        if kind=='missing':db.execute(text('DELETE FROM driver_locations'))
        elif kind=='stale':db.execute(text("UPDATE driver_locations SET captured_at=captured_at-interval '3 minutes'"))
        else:db.execute(text("UPDATE driver_locations SET data=jsonb_set(data,'{accuracy_m}','200')"))
    order=new_order(w);decision=check(suggest(w['client'],order))
    assert not decision['candidates']
    assert any('GPS' in row['reason'] for row in decision['excluded'])
    assert check(w['mobile'].get(BASE+f'/driver/routes/{route["id"]}'))['version']==route['version']


def test_live_insert_refuses_oversize_load_and_open_issues(fleet):
    w=fleet;_,route=active(w)
    body=booking(w['shipper']['id'],w['service'],w['type_id']);body['items'][0]['weight_kg']='1200'
    order=check(post(w['client'],BASE+'/orders',body),201)
    assert not check(suggest(w['client'],order))['candidates']
    order=new_order(w)
    check(post(w['mobile'],BASE+f'/driver/stops/{route["stops"][0]["stop_id"]}/issue',{'kind':'VEHICLE','description':'Truck needs inspection'}),201)
    decision=check(suggest(w['client'],order))
    assert not decision['candidates'] and any('issue' in r['reason'] for r in decision['excluded'])


def test_live_revalidates_gps_after_recommendation_and_rolls_back(fleet):
    w=fleet;_,route=active(w);order=new_order(w);decision=check(suggest(w['client'],order))
    with owner_engine.begin() as db:db.execute(text("UPDATE driver_locations SET captured_at=captured_at-interval '3 minutes'"))
    assert approve(w,decision).status_code==409
    assert check(w['client'].get(BASE+f'/orders/{order["id"]}'))['status']=='NEW'
    assert check(w['mobile'].get(BASE+f'/driver/routes/{route["id"]}'))['version']==route['version']


def test_live_direct_assignment_explicit_opt_in_and_tenant_review_boundaries(fleet):
    w=fleet;_,route=active(w);order=new_order(w)
    body=dict(version=order['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],
        route_id=route['id'],route_version=route['version'],planned_at=route['planned_at'])
    assert post(w['client'],BASE+f'/orders/{order["id"]}/assign',body).status_code==409
    updated=check(post(w['client'],BASE+f'/orders/{order["id"]}/assign',body|{'allow_live':True}))['route']
    ack=dict(version=updated['version'],generation=updated['generation'])
    assert post(w['client'],BASE+f'/driver/routes/{route["id"]}/acknowledge',ack).status_code==403
    with TestClient(app,headers=HEADERS) as other:
        login(other);company(other,'other');login(other,'dispatch','other','dispatcher')
        assert post(other,'/api/v1/companies/other/orders/'+order['id']+'/assign',body|{'allow_live':True}).status_code==404
    with TestClient(app,headers=HEADERS) as shipper:
        login(shipper,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        assert shipper.post(BASE+'/dispatch-plan').status_code==403


def test_dispatch_zones_are_separate_preserved_and_overlap_rejected(fleet):
    w=fleet;client=w['client'];current=check(client.get(BASE+'/settings'))
    zones=[{'name':'South','postal_codes':['V5Y']}]
    saved=check(client.put(BASE+'/settings',json={'version':current['version'],'data':current['data']|{'dispatch_zones':zones}},headers={'Idempotency-Key':str(uuid4())}))
    legacy={k:v for k,v in saved['data'].items() if k!='dispatch_zones'}
    again=check(client.put(BASE+'/settings',json={'version':saved['version'],'data':legacy},headers={'Idempotency-Key':str(uuid4())}))
    assert again['data']['dispatch_zones']==zones
    order=new_order(w);decision=check(suggest(client,order));assert decision['candidates'][0]['metrics']['zone']=='South'
    bad=again['data']|{'dispatch_zones':zones+[{'name':'Other','postal_codes':['V5Y 1V4']}]}
    assert client.put(BASE+'/settings',json={'version':again['version'],'data':bad},headers={'Idempotency-Key':str(uuid4())}).status_code==422
    config=SettingsData.model_validate(again['data'])
    assert zone(address(postal='V6B 1A1'),config).startswith('Area ')


def test_fleet_proposal_has_no_assignment_side_effect_and_approvals_revalidate(fleet):
    w=fleet;first=new_order(w);second=new_order(w)
    plan=check(w['client'].post(BASE+'/dispatch-plan'))
    assert {r['order_id'] for r in plan}=={first['id'],second['id']}
    assert check(w['client'].get(BASE+'/routes'))==[]
    assert any(row['candidates'][0]['metrics']['route_orders']==1 for row in plan)
    for row in plan:check(approve(w,row))
    routes=check(w['client'].get(BASE+'/routes'));assert len(routes)==1 and len(routes[0]['stops'])==4


def test_auto_live_insertion_waits_for_review_then_retries_on_ack(fleet):
    w=fleet;_,route=active(w);mode(w['client'],'AUTO');new_order(w)
    assert service.dispatch_pending(service.auto_companies()[0])['ASSIGNED']==1
    later=new_order(w)
    assert service.dispatch_pending(service.auto_companies()[0])['NO_CANDIDATE']==1
    mine=check(w['mobile'].get(BASE+f'/driver/routes/{route["id"]}'))
    check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/acknowledge',dict(version=mine['version'],generation=mine['generation'])))
    # Acknowledgement wakes the waiting Order, without waiting for the five-minute fallback.
    assert service.dispatch_pending(service.auto_companies()[0])['ASSIGNED']==1
    assert check(w['client'].get(BASE+f'/orders/{later["id"]}'))['route_id']==route['id']


def test_movement_handles_approaching_departing_stationary_and_unknown():
    def point(lat,seconds):return SimpleNamespace(captured_at=now()-timedelta(seconds=seconds),data={'latitude':lat,'longitude':-123.11,'accuracy_m':10})
    target=address(lat=49.30)
    assert movement([point(49.27,0),point(49.26,30)],target)[0]=='APPROACHING'
    assert movement([point(49.26,0),point(49.27,30)],target)[0]=='MOVING_AWAY'
    assert movement([point(49.26,0),point(49.26,30)],target)[0]=='STATIONARY'
    assert movement([point(49.26,0)],target)==('UNKNOWN',None)


def test_routing_uses_confirmed_load_and_protects_next_stop():
    current=now();vehicle=dict(payload_kg=100,volume_m3=10,pallet_capacity=2,length_cm=300,width_cm=200,height_cm=200)
    stops=[dict(id='new',kind='PICKUP',address=address()),dict(id='old-delivery',kind='DROPOFF',address=address()),dict(id='new-delivery',kind='DROPOFF',address=address())]
    items=[dict(id='old',quantity=1,weight_kg=90,length_cm=20,width_cm=20,height_cm=20,pickup_id='done',delivery_id='old-delivery'),
           dict(id='new-item',quantity=1,weight_kg=20,length_cm=20,width_cm=20,height_cm=20,pickup_id='new',delivery_id='new-delivery')]
    result=optimize(stops,items,vehicle,current,None,480,initial_load=[90,.008,0],visited={'done'},first_stop='old-delivery')
    assert result['stops'][0]['stop_id']=='old-delivery' and result['stops'][-1]['load_kg']=='0'
    from fastapi import HTTPException
    with pytest.raises(HTTPException):optimize(stops,items,vehicle,current,None,480,initial_load=[90,.008,0],visited={'done'},first_stop='new')


def test_heading_and_road_matrix_use_direction_and_chunk_large_routes(monkeypatch):
    from app.operations import travel
    calls=[]
    def provider(db,actor,method,payload,mask):
        calls.append(payload)
        return [{'originIndex':i,'destinationIndex':j,'condition':'ROUTE_EXISTS','duration':'60s'}
            for i in range(len(payload['origins'])) for j in range(len(payload['destinations']))]
    monkeypatch.setenv('ROUTING_PROVIDER','google');monkeypatch.setattr(travel,'google',provider)
    db=SimpleNamespace(info={})
    stops=[{'id':str(i),'address':address()|({'heading':180} if i==0 else {})} for i in range(27)]
    result=travel.matrix(db,None,stops)
    assert len(calls)==4 and len(result)==27*27
    assert calls[0]['origins'][0]['waypoint']['location']['heading']==180
    assert all(len(c['origins'])*len(c['destinations'])<=625 for c in calls)
    assert travel.matrix(db,None,stops)==result and len(calls)==4


def test_equivalent_work_is_distributed_across_available_drivers(fleet):
    w=fleet;client=w['client'];other=w['omar']
    body={k:other[k] for k in ('name','email','phone','address','vehicle_id')}
    check(client.put(BASE+f'/drivers/{other["id"]}',json={'version':other['version'],'data':body,'duty_status':'ON_DUTY','expected_on_duty':False},headers={'Idempotency-Key':str(uuid4())}))
    # Different delivery areas: opening another driver's route avoids repeated cross-zone travel.
    for lat in (49.27,49.28,49.38,49.39):
        raw=booking(w['shipper']['id'],w['service'],w['type_id']);raw['stops'][1]['address']=address(lat=lat)
        check(post(client,BASE+'/orders',raw),201)
    rows=check(client.post(BASE+'/dispatch-plan'))
    chosen=[r['candidates'][0] for r in rows]
    assert len({c['driver_id'] for c in chosen})==2
    assert all(c['metrics']['worked_minutes']>=0 and c['metrics']['remaining_minutes']>0 for c in chosen)


def test_expected_roster_reserves_tentatively_without_assigning_off_duty(fleet):
    w=fleet;client=w['client'];other=w['omar']
    body={k:other[k] for k in ('name','email','phone','address','vehicle_id')}
    body.update(shift_start=(now()+timedelta(minutes=20)).isoformat(),shift_end=(now()+timedelta(hours=8)).isoformat())
    check(client.put(BASE+f'/drivers/{other["id"]}',json={'version':other['version'],'data':body},headers={'Idempotency-Key':str(uuid4())}))
    for _ in range(4):new_order(w)
    proposals=check(client.post(BASE+'/dispatch-plan'))
    expected=[row for row in proposals if row['candidates'] and row['candidates'][0]['metrics']['awaiting_duty']]
    assert expected
    row=expected[0]
    assert post(client,BASE+f'/dispatch-decisions/{row["id"]}/approve',{'version':row['version'],'driver_id':other['id']}).status_code==409
    assert all(o['status']=='NEW' for o in check(client.get(BASE+'/orders')))


def test_concurrent_live_insertions_keep_one_review_boundary(fleet):
    from concurrent.futures import ThreadPoolExecutor
    w=fleet;_,route=active(w)
    orders=[new_order(w),new_order(w)]
    cookies=dict(w['client'].cookies)
    def insert(order):
        with TestClient(app,headers=HEADERS,cookies=cookies) as client:
            return post(client,BASE+f'/orders/{order["id"]}/assign',dict(version=order['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],
                route_id=route['id'],route_version=route['version'],planned_at=route['planned_at'],allow_live=True)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(insert,orders))
    assert sorted(codes)==[200,409]
    mine=check(w['mobile'].get(BASE+f'/driver/routes/{route["id"]}'))
    assert len(mine['stops'])==4 and mine['plan']['requires_acknowledgement']


def test_legacy_driver_app_keeps_existing_work_but_cannot_receive_live_changes(fleet):
    w=fleet;first=new_order(w);route=assign(w,first)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    gps(w);second=new_order(w)
    assert not check(suggest(w['client'],second))['candidates']
    # Updated web/native explicit review enables the capability without interrupting current custody.
    reviewed=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/acknowledge',dict(version=route['version'],generation=route['generation'])))
    assert reviewed['plan']['live_updates_supported'] and reviewed['generation']==route['generation']
    assert check(suggest(w['client'],second,True))['candidates']
