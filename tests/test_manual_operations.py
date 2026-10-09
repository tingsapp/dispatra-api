"""Real PostgreSQL acceptance for the manual dispatcher/shipper/driver workflow."""
import base64
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from conftest import login, company, post, HEADERS, owner_engine

BASE = '/api/v1/companies/acme'


def check(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def address(city='Vancouver', postal='V5Y 1V4', lat=49.26, lng=-123.11):
    return dict(text=f'123 Main Street, {city}, BC {postal}, Canada',city=city,province='BC',postal_code=postal,latitude=lat,longitude=lng)


def booking(shipper, service, vehicle_type, rate=None):
    pickup, delivery, item = [str(uuid4()) for _ in range(3)]
    return dict(shipper_id=shipper,rate_card_id=rate,service_id=service,vehicle_type_id=vehicle_type,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        stops=[dict(id=pickup,kind='PICKUP',address=address()),dict(id=delivery,kind='DROPOFF',address=address(lat=49.27))],
        items=[dict(id=item,pickup_id=pickup,delivery_id=delivery,quantity=1,weight_kg='10',length_cm='20',width_cm='20',height_cm='20')])


@pytest.fixture
def setup(client):
    login(client); company(client); login(client,'dispatch','acme','dispatcher')
    catalog = check(client.get(BASE+'/catalog'))
    rates = check(client.get(BASE+'/rate-cards'))
    type_id = next(c['id'] for c in catalog if c['code']=='veh_1_ton')
    service = next(c['id'] for c in catalog if c['code']=='SAME_DAY')
    fixed = next(c['id'] for c in rates if c['data']['method']=='FIXED')
    shipper_body = dict(name='Alice Shipper',kind='BUSINESS',company_name='Fresh Test',email='alice@example.com',phone='6045550101',warehouse=address(),rate_card_id=fixed)
    shipper = check(post(client,BASE+'/shippers',shipper_body),201)
    vehicle = check(post(client,BASE+'/vehicles',dict(name='Test Van',type_id=type_id,plate='TEST123',province='BC',payload_kg=1000,volume_m3=12,length_cm=300,width_cm=180,height_cm=180,pallet_capacity=2)),201)
    driver_body = dict(name='Dana Driver',email='dana@example.com',phone='6045550102',address=address(),vehicle_id=vehicle['id'])
    driver = check(post(client,BASE+'/drivers',driver_body),201)
    with TestClient(app,headers=HEADERS) as mobile:
        login(mobile,'driver','acme','dana@example.com',driver['initial_password'])
        duty = check(post(mobile,BASE+'/driver/duty',{}),201)
        yield dict(client=client,mobile=mobile,catalog=catalog,rates=rates,type_id=type_id,service=service,
            fixed=fixed,shipper=shipper,shipper_body=shipper_body,vehicle=vehicle,driver=driver,driver_body=driver_body,duty=duty)


def new_order(w, **updates):
    body = booking(w['shipper']['id'],w['service'],w['type_id']);body.update(updates)
    return check(post(w['client'],BASE+'/orders',body),201)


def assign(w, order, route=None):
    body=dict(version=order['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat())
    if route: body.update(route_id=route['id'],route_version=route['version'])
    return check(post(w['client'],BASE+f'/orders/{order["id"]}/assign',body))['route']


def finish(w, route):
    mobile=w['mobile']
    route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    manifest=str(route)
    assert 'subtotal' not in manifest and 'internal_notes' not in manifest and 'PricingSnapshot' not in manifest
    for visit in list(route['stops']):
        route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=datetime.now(timezone.utc).isoformat())))
        data=dict(version=route['version'],generation=route['generation'],captured_at=datetime.now(timezone.utc).isoformat(),
            quantities={item['id']:item['quantity'] for item in route['items'] if item['pickup_id' if visit['stop']['kind']=='PICKUP' else 'delivery_id']==visit['stop_id']})
        if visit['stop']['kind']=='DROPOFF':
            evidence=check(post(mobile,BASE+f'/driver/stops/{visit["stop_id"]}/evidence',dict(kind='SIGNATURE',media_type='image/png',
                captured_at=datetime.now(timezone.utc).isoformat(),content_base64=base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'evidence-data').decode())),201)
            assert any(item['id']==evidence['id'] for item in check(mobile.get(BASE+f'/driver/stops/{visit["stop_id"]}/evidence')))
            data.update(recipient_name='Receiver',evidence_ids=[evidence['id']])
        route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/stops/{visit["id"]}/complete',data))
    return check(post(mobile,BASE+f'/driver/routes/{route["id"]}/finish',dict(version=route['version'],generation=route['generation'])))


def test_manual_delivery_completion_and_immutable_pricing(setup):
    w=setup;client=w['client']
    order=new_order(w)
    assert order['pricing']['total']=='128.18'
    route=assign(w,order)
    settings=check(client.get(BASE+'/settings')); settings['data']['gst_percent']='20'
    check(client.put(BASE+'/settings',json={'version':settings['version'],'data':settings['data']},headers={'Idempotency-Key':str(uuid4())}))
    assert finish(w,route)['status']=='COMPLETED'
    done=check(client.get(BASE+f'/orders/{order["id"]}'))
    assert done['status']=='COMPLETED'
    assert done['pricing']==order['pricing']
    assert client.get(BASE+'/invoices').status_code==404
    assert post(client,BASE+f'/orders/{order["id"]}/invoice',{'version':done['version']}).status_code==404
    activity=check(client.get(BASE+f'/drivers/{w["driver"]["id"]}/activity'))
    assert activity['completed_orders']==1 and 'estimated_payout' not in activity
    report=check(client.get(BASE+'/analytics'))
    assert report['completed_orders']==1
    assert report['rows'][0]['shipper_name'] and report['rows'][0]['driver_name']
    assert report['rows'][0]['pod_verified'] is True
    assert report['rows'][0]['service_name'] and report['rows'][0]['vehicle_unit']
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from email_deliveries'))==0
        assert db.scalar(text('select count(*) from pricing_revisions'))==0


def test_shipper_booking_and_role_isolation(setup):
    w=setup
    with TestClient(app,headers=HEADERS) as portal:
        login(portal,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        profile=check(portal.get(BASE+'/shipper/profile'))
        changed=check(portal.patch(BASE+'/shipper/profile',json=dict(version=profile['version'],contact_name='Alice Updated',phone='6045550199',warehouse=address('Burnaby','V5J 1A1')),headers={'Idempotency-Key':str(uuid4())}))
        assert changed['name']=='Alice Updated' and changed['phone']=='6045550199' and changed['warehouse']['city']=='Burnaby'
        assert check(portal.get(BASE+'/profile'))['contact_name']=='Alice Updated'
        payload=booking(None,w['service'],w['type_id'])
        own=check(post(portal,BASE+'/orders',payload),201)
        assert own['shipper_id']==w['shipper']['id'] and own['source']=='SHIPPER_PORTAL'
        assert own['pricing']['context']=={}
        assert portal.get(BASE+'/drivers').status_code==403
        assert post(portal,BASE+f'/orders/{own["id"]}/assign',dict(version=1,driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat())).status_code==403
        assert post(portal,BASE+'/orders',{**payload,'shipper_id':str(uuid4())}).status_code==404
        assert post(portal,BASE+'/orders',{**payload,'distance_km':1}).status_code==403
        assert len(check(portal.get(BASE+'/orders')))==1
    assert w['mobile'].get(BASE+'/orders').status_code==403
    assert w['mobile'].get(BASE+'/invoices').status_code==404


def test_shipper_preferred_driver_and_rate_card(setup):
    w=setup;client=w['client']
    default=next(r for r in w['rates'] if r['is_default'])
    plain=check(post(client,BASE+'/shippers',{**w['shipper_body'],'email':'bob@example.com','rate_card_id':None}),201)
    assert plain['rate_card_id']==default['id'] and plain['rate_card_name']==default['data']['name']
    with TestClient(app,headers=HEADERS) as portal:
        login(portal,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        assert check(portal.get(BASE+'/shipper/profile'))['rate_card_name']==next(r for r in w['rates'] if r['id']==w['fixed'])['data']['name']
        prefs=check(portal.get(BASE+'/booking-preferences'))
        assert prefs['gst_enabled'] is True and float(prefs['gst_percent'])==5 and prefs['fuel_enabled'] is True and 'provincial_percent' in prefs
        drivers=check(portal.get(BASE+'/booking-drivers'))
        assert {'id':w['driver']['id'],'name':'Dana Driver'} in drivers and all(set(d)=={'id','name'} for d in drivers)
        fresh=lambda **extra: {**booking(None,w['service'],w['type_id']),**extra}
        assert check(post(portal,BASE+'/orders',fresh()),201)['facts']['preferred_driver_id'] is None
        own=check(post(portal,BASE+'/orders',fresh(preferred_driver_id=w['driver']['id'])),201)
        assert own['facts']['preferred_driver_id']==w['driver']['id'] and own['status']=='NEW' and own['route_id'] is None
        assert own['pricing']['rate_card_id']==w['fixed']
        assert post(portal,BASE+'/orders',fresh(preferred_driver_id=str(uuid4()))).status_code==422
        assert portal.get(BASE+'/drivers').status_code==403
    # The preference is kept through dispatcher edits and never blocks assigning someone else.
    edited=check(client.put(BASE+f'/orders/{own["id"]}',json={'version':own['version'],'booking':own['facts']},headers={'Idempotency-Key':str(uuid4())}))
    assert edited['facts']['preferred_driver_id']==w['driver']['id']


def test_seed_is_repeatable_and_preserves_changes(setup):
    w=setup;client=w['client']
    default=next(r for r in w['rates'] if r['is_default'])
    default['data']['base_fee']='123'
    updated=check(client.put(BASE+'/rate-cards/'+default['id'],json={'version':default['version'],'is_default':True,'active':True,'data':default['data']},headers={'Idempotency-Key':str(uuid4())}))
    assert check(post(client,BASE+'/pricing/seed',{}))['created']==0
    rates=check(client.get(BASE+'/rate-cards'))
    assert next(r for r in rates if r['id']==updated['id'])['data']['base_fee']=='123'
    assert len(rates)==6
    zone=next(r for r in rates if r['data']['method']=='ZONE')
    assert zone['data']['weight_bands'][0]['prices']['zone_1']=='20'
    assert Decimal(zone['data']['weight_bands'][0]['to_kg'])==Decimal('44.90564463')
    vehicles={c['code']:c for c in check(client.get(BASE+'/catalog')) if c['kind']=='VEHICLE_TYPE'}
    assert set(vehicles)=={'veh_1_ton','veh_2_ton','veh_3_ton','veh_5_ton','REFRIGERATED_VAN','REFRIGERATED_TRUCK','FLATBED_TRUCK','DRY_VAN_53FT','REFRIGERATED_TRAILER','FLATBED_TRAILER'}
    assert vehicles['REFRIGERATED_VAN']['data']['equipment']==['REFRIGERATION'] and vehicles['FLATBED_TRAILER']['data']['equipment']==['OPEN_DECK']
    assert vehicles['veh_3_ton']['data']['equipment']==['LIFTGATE'] and not vehicles['veh_5_ton']['active']
    assert Decimal(vehicles['DRY_VAN_53FT']['data']['length_cm'])==Decimal('1600.2') and vehicles['DRY_VAN_53FT']['data']['pallet_capacity']==26


def test_atomic_accounts_and_idempotency_do_not_store_passwords(setup):
    w=setup;client=w['client'];key=str(uuid4())
    body={**w['shipper_body'],'email':'second@example.com'}
    first=check(post(client,BASE+'/shippers',body,key),201)
    replay=check(post(client,BASE+'/shippers',body,key),201)
    assert first['id']==replay['id'] and first['initial_password'] and replay['initial_password'] is None
    assert post(client,BASE+'/shippers',{**body,'name':'Changed'},key).status_code==409
    assert post(client,BASE+'/shippers',body).status_code==409
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from shippers'))==2
        stored=str(db.execute(text('select result from operations')).all())
        assert first['initial_password'] not in stored
        assert 'initial_password' not in stored


def test_assignment_capacity_city_dg_and_stale_version(setup):
    w=setup;order=new_order(w)
    body=dict(version=99,driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat())
    assert post(w['client'],BASE+f'/orders/{order["id"]}/assign',body).status_code==409
    payload=booking(w['shipper']['id'],w['service'],w['type_id'])
    payload['items'][0]['weight_kg']=1200
    heavy=check(post(w['client'],BASE+'/orders',payload),201)
    assert post(w['client'],BASE+f'/orders/{heavy["id"]}/assign',{**body,'version':1}).status_code==409
    payload=booking(w['shipper']['id'],w['service'],w['type_id']);payload['items'][0]['dangerous_goods']=True
    dg=check(post(w['client'],BASE+'/orders',payload),201)
    assert post(w['client'],BASE+f'/orders/{dg["id"]}/assign',{**body,'version':1}).status_code==409
    payload=booking(w['shipper']['id'],w['service'],w['type_id']);payload['stops'][0]['address']=address(city='Burnaby')
    outside=check(post(w['client'],BASE+'/orders',payload),201)
    assert post(w['client'],BASE+f'/orders/{outside["id"]}/assign',{**body,'version':1}).status_code==409


def test_multiple_orders_route_and_locked_mutations(setup):
    w=setup;one=new_order(w);two=new_order(w)
    route=assign(w,one);original_ids={v['stop_id']:v['id'] for v in route['stops']}
    without_route=post(w['client'],BASE+f'/orders/{two["id"]}/assign',dict(version=two['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat()))
    assert without_route.status_code==409 and 'planned Route' in without_route.json()['error']['message']
    route=assign(w,two,route)
    assert len(route['stops'])==4
    assert all(original_ids.get(v['stop_id'],v['id'])==v['id'] for v in route['stops'])
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    third=new_order(w)
    in_progress=post(w['client'],BASE+f'/orders/{third["id"]}/assign',dict(version=third['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat()))
    assert in_progress.status_code==409 and 'in-progress Route' in in_progress.json()['error']['message']
    body=dict(version=1,driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],route_id=route['id'],route_version=route['version'],planned_at=datetime.now(timezone.utc).isoformat())
    assert post(w['client'],BASE+f'/orders/{third["id"]}/assign',body).status_code==409
    assert post(w['client'],BASE+f'/routes/{route["id"]}/release',dict(version=route['version'],generation=route['generation'])).status_code==409


def test_pod_precedence_and_offline_retry(setup):
    w=setup;order=new_order(w);route=assign(w,order)
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    first,last=route['stops'];item=route['items'][0]
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{first["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=datetime.now(timezone.utc).isoformat())))
    payload=dict(version=route['version'],generation=route['generation'],captured_at=datetime.now(timezone.utc).isoformat(),quantities={item['id']:1})
    assert post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{last["id"]}/complete',payload).status_code==409
    key=str(uuid4());url=BASE+f'/driver/routes/{route["id"]}/stops/{first["id"]}/complete'
    updated=check(post(w['mobile'],url,payload,key))
    assert check(post(w['mobile'],url,payload,key))['version']==updated['version']
    updated=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{last["id"]}/arrive',dict(version=updated['version'],generation=updated['generation'],captured_at=datetime.now(timezone.utc).isoformat())))
    payload['version']=updated['version'];payload['captured_at']=datetime.now(timezone.utc).isoformat()
    assert post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/stops/{last["id"]}/complete',payload).status_code==409
    assert post(w['client'],BASE+f'/orders/{order["id"]}/invoice',{'version':3}).status_code==404


def test_duty_boundary_and_quote_review(setup):
    w=setup;mobile=w['mobile'];duty=w['duty']
    before=datetime.now(timezone.utc)
    check(post(mobile,BASE+'/driver/duty/'+duty['id']+'/end',dict(version=duty['version'],ended_at=before.isoformat())))
    data=dict(duty_id=duty['id'],captured_at=datetime.now(timezone.utc).isoformat(),latitude=49.2,longitude=-123.1,accuracy_m=5,location_permission='GRANTED')
    assert post(mobile,BASE+'/driver/location',data).status_code==409
    assert post(mobile,BASE+'/driver/location',{**data,'captured_at':before.isoformat()}).status_code==201
    unavailable=new_order(w)
    assignment=dict(version=unavailable['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat())
    rejected=post(w['client'],BASE+f'/orders/{unavailable["id"]}/assign',assignment)
    assert rejected.status_code==409 and 'On Duty' in rejected.json()['error']['message']
    distance=next(r['id'] for r in w['rates'] if r['data']['method']=='BASE_PLUS_DISTANCE' and r['active'])
    order=new_order(w,rate_card_id=distance)
    assert order['pricing']['status']=='NEEDS_ATTENTION'
    assert order['pricing']['total'] is None


def test_cross_company_foreign_keys_and_rls(setup):
    w=setup;order=new_order(w);client=w['client']
    login(client);other=company(client,'other');login(client,'dispatch','other','dispatcher')
    assert client.get('/api/v1/companies/other/orders/'+order['id']).status_code==404
    assert client.get(BASE+'/orders').status_code==404
    from app.database import engine
    with engine.connect() as db,db.begin():
        db.execute(text("select set_config('app.organization_id', :org, true)"),{'org':other['id']})
        assert db.scalar(text('select count(*) from orders'))==0
        assert db.scalar(text('select count(*) from shippers'))==0


def test_concurrent_assignment_and_logout_closes_duty(setup):
    from concurrent.futures import ThreadPoolExecutor
    w=setup;order=new_order(w)
    payload=dict(version=order['version'],driver_id=w['driver']['id'],vehicle_id=w['vehicle']['id'],planned_at=datetime.now(timezone.utc).isoformat())
    cookie=w['client'].cookies.get('dispatra_session')
    def request_assignment(_):
        with TestClient(app,headers=HEADERS,cookies={'dispatra_session':cookie}) as client:
            return post(client,BASE+f'/orders/{order["id"]}/assign',payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(request_assignment,range(2)))
    assert sorted(r.status_code for r in results)==[200,409]
    assert w['mobile'].post('/api/v1/auth/logout').status_code==204
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from duty_sessions where ended_at is null'))==0
        assert db.scalar(text('select count(*) from routes'))==1


def test_web_projections_quotes_and_stop_issue_scope(setup):
    w=setup; client=w['client']; mobile=w['mobile']
    with TestClient(app,headers=HEADERS) as portal:
        login(portal,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        preferences=check(portal.get(BASE+'/booking-preferences'))
        assert set(preferences)=={'currency','time_zone','weight_unit','dimension_unit','distance_unit','gst_enabled','gst_percent','provincial_enabled','provincial_percent','fuel_enabled','fuel_percent','default_service_id'}
        assert preferences['weight_unit']=='lb'
        assert portal.get(BASE+'/quotes').status_code==403
    first=check(post(client,BASE+'/quotes',booking(None,w['service'],w['type_id'],w['fixed'])),201)
    second=check(post(client,BASE+'/quotes',booking(None,w['service'],w['type_id'],w['fixed'])),201)
    page=check(client.get(BASE+'/quotes?limit=1'));assert len(page)==1
    rest=check(client.get(BASE+'/quotes',params={'after':page[0]['id']}))
    assert {q['id'] for q in page+rest}=={first['id'],second['id']}
    assert check(client.get(BASE+'/orders'))==[]
    order=new_order(w);stop=order['facts']['stops'][0]['id']
    assert post(mobile,BASE+f'/driver/stops/{stop}/issue',{'kind':'OTHER','description':'Cannot claim another order'}).status_code==404
    route=assign(w,order)
    issue=check(post(mobile,BASE+f'/driver/stops/{stop}/issue',{'kind':'OTHER','description':'Gate is temporarily closed'}),201)
    assert issue['order_id']==order['id'] and issue['stop_id']==stop
    monitor=check(client.get(BASE+'/monitor'));assert monitor['needs_attention'][0]['id']==issue['id']
    assert next(row for row in monitor['drivers'] if row['id']==w['driver']['id'])['on_duty'] is True
    check(post(client,BASE+f'/issues/{issue["id"]}/resolve',{'version':issue['version'],'resolution':'Gate opened'}))
    finish(w,route)
    proof=check(client.get(BASE+f'/orders/{order["id"]}/delivery-proof'))
    assert len(proof)==1 and proof[0]['recipient_name']=='Receiver'
    assert proof[0]['evidence'][0]['kind']=='SIGNATURE'
    assert check(client.get(BASE+'/analytics'))['pod_verified_orders']==1


def test_dispatcher_driver_edit_saves_duty_without_starting_tracking(setup):
    w = setup
    client, mobile = w['client'], w['mobile']
    identity = w['driver']['id']
    initial = check(client.get(BASE + f'/drivers/{identity}'))
    assert initial['on_duty'] is True
    with owner_engine.connect() as db:
        locations_before = db.scalar(text('select count(*) from driver_locations'))
    body = {'version': initial['version'], 'data': w['driver_body'], 'duty_status': 'OFF_DUTY', 'expected_on_duty': True}
    off = check(client.put(BASE + f'/drivers/{identity}', json=body, headers={'Idempotency-Key': str(uuid4())}))
    assert off['on_duty'] is False
    assert next(row for row in check(client.get(BASE+'/drivers')) if row['id'] == identity)['on_duty'] is False
    assert check(mobile.get(BASE+'/driver/profile'))['duty'] is None
    assert check(client.get(BASE+'/monitor'))['drivers'][0]['on_duty'] is False
    body['version'], body['duty_status'], body['expected_on_duty'] = off['version'], 'ON_DUTY', False
    on = check(client.put(BASE + f'/drivers/{identity}', json=body, headers={'Idempotency-Key': str(uuid4())}))
    assert on['on_duty'] is True
    assert check(mobile.get(BASE+'/driver/profile'))['duty'] is not None
    assert check(client.get(BASE+'/monitor'))['drivers'][0]['on_duty'] is True
    assert on['location_permission'] == initial['location_permission']
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from driver_locations')) == locations_before
    assert mobile.put(BASE + f'/drivers/{identity}', json=body, headers={'Idempotency-Key': str(uuid4())}).status_code == 403
    stale_duty = {**body, 'version': on['version']}
    assert client.put(BASE + f'/drivers/{identity}', json=stale_duty, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    assert check(client.get(BASE + f'/drivers/{identity}'))['on_duty'] is True
    assert client.put(BASE + f'/drivers/{identity}', json=body, headers={'Idempotency-Key': str(uuid4())}).status_code == 409


def test_dispatcher_completes_assigned_order_without_driver_pod(setup):
    w=setup;client=w['client']
    first,second=new_order(w),new_order(w)
    assert check(post(client,BASE+f'/orders/{first["id"]}/complete',{'version':first['version']}),409)
    route=assign(w,first)
    first=check(client.get(BASE+f'/orders/{first["id"]}'))
    route=assign(w,check(client.get(BASE+f'/orders/{second["id"]}')),route)
    check(post(client,BASE+f'/orders/{first["id"]}/complete',{'version':first['version']-1}),409)
    key=str(uuid4())
    done=check(post(client,BASE+f'/orders/{first["id"]}/complete',{'version':first['version']},key))
    assert done['status']=='COMPLETED' and done['completed_at']
    assert check(post(client,BASE+f'/orders/{first["id"]}/complete',{'version':first['version']},key))['id']==done['id']
    route=next(r for r in check(client.get(BASE+'/routes')) if r['id']==route['id'])
    assert route['status']=='PLANNED'
    second=check(client.get(BASE+f'/orders/{second["id"]}'))
    check(post(client,BASE+f'/orders/{second["id"]}/complete',{'version':second['version']}))
    route=next(r for r in check(client.get(BASE+'/routes')) if r['id']==route['id'])
    assert route['status']=='COMPLETED' and all(visit['status']=='COMPLETED' for visit in route['stops'])
    assert check(post(w['mobile'],BASE+f'/orders/{first["id"]}/complete',{'version':done['version']}),403)
    proof=check(client.get(BASE+f'/orders/{first["id"]}/delivery-proof'))
    assert len(proof)==1 and proof[0]['completed_by_dispatcher'] is True and proof[0]['evidence']==[]
    assert done['pricing']==first['pricing']


def test_hourly_dispatcher_completion_keeps_quoted_price_without_invoice_review(setup):
    w=setup;client=w['client']
    hourly=next(r['id'] for r in w['rates'] if r['data']['method']=='HOURLY')
    order=new_order(w,rate_card_id=hourly,estimated_minutes=60)
    assert order['pricing']['status']=='PRICED'
    assign(w,order)
    assigned=check(client.get(BASE+f'/orders/{order["id"]}'))
    done=check(post(client,BASE+f'/orders/{order["id"]}/complete',{'version':assigned['version']}))
    assert done['status']=='COMPLETED' and done['pricing']==order['pricing']
    assert not any(item['order_id']==order['id'] for item in check(client.get(BASE+'/monitor'))['needs_attention'])
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from email_deliveries'))==0


def test_driver_orders_profile_and_viewable_proof(setup):
    w=setup;client=w['client'];mobile=w['mobile']
    profile=check(mobile.get(BASE+'/driver/profile'))
    assert profile['number'] and profile['vehicle_name']=='Test Van · TEST123'
    updated=check(mobile.patch(BASE+'/driver/profile',json={'version':profile['version'],'phone':'604 555 0199'},headers={'Idempotency-Key':str(uuid4())}))
    assert updated['phone']=='604 555 0199' and updated['version']==profile['version']+1
    assert mobile.patch(BASE+'/driver/profile',json={'version':profile['version'],'phone':'6045550000'},headers={'Idempotency-Key':str(uuid4())}).status_code==409
    assert client.patch(BASE+'/driver/profile',json={'version':1,'phone':'6045550000'},headers={'Idempotency-Key':str(uuid4())}).status_code==403
    unassigned=new_order(w);order=new_order(w)
    assert check(mobile.get(BASE+'/driver/orders'))==[]
    route=assign(w,order)
    listed=check(mobile.get(BASE+'/driver/orders'))
    assert [row['id'] for row in listed]==[order['id']] and listed[0]['status']=='ASSIGNED' and listed[0]['route_status']=='PLANNED'
    assert listed[0]['service_name'] and [s['kind'] for s in listed[0]['stops']]==['PICKUP','DROPOFF']
    assert listed[0]['shipper']=={'name':'Alice Shipper','company_name':'Fresh Test','phone':'6045550101','email':'alice@example.com'}
    assert 'warehouse' not in listed[0]['shipper']
    assert not {'pricing','booking','facts','subtotal','internal_notes'} & set(listed[0]) and 'subtotal' not in str(listed)
    finish(w,route)
    assert check(mobile.get(BASE+'/driver/orders'))[0]['status']=='COMPLETED'
    assert unassigned['id'] not in {row['id'] for row in check(mobile.get(BASE+'/driver/orders'))}
    proof=check(client.get(BASE+f'/orders/{order["id"]}/delivery-proof'))
    assert proof[0]['completed_by_dispatcher'] is False and [e['kind'] for e in proof[0]['evidence']]==['SIGNATURE']
    image=client.get(BASE+f'/evidence/{proof[0]["evidence"][0]["id"]}')
    assert image.status_code==200 and image.headers['content-type']=='image/png' and image.headers['content-disposition'].startswith('inline')
    assert image.content.startswith(b'\x89PNG')
    with TestClient(app,headers=HEADERS) as portal:
        login(portal,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        assert check(portal.get(BASE+f'/orders/{order["id"]}/delivery-proof'))[0]['evidence'][0]['id']==proof[0]['evidence'][0]['id']
        assert portal.get(BASE+f'/evidence/{proof[0]["evidence"][0]["id"]}').status_code==200
        assert portal.get(BASE+'/driver/orders').status_code==403


def test_order_tracking_live_location_privacy(setup):
    w=setup;client=w['client'];mobile=w['mobile']
    other=check(post(client,BASE+'/shippers',{**w['shipper_body'],'name':'Bob Shipper','company_name':'Other Co','email':'bob@example.com'}),201)
    stamp=lambda: datetime.now(timezone.utc).isoformat()
    locate=lambda: check(post(mobile,BASE+'/driver/location',dict(duty_id=w['duty']['id'],captured_at=stamp(),latitude=49.2601,longitude=-123.1101,accuracy_m=8,location_permission='GRANTED')),201)
    start=lambda route: check(post(mobile,BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    def complete_next(route):
        nxt=next(v for v in route['stops'] if v['status']!='COMPLETED')
        route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/stops/{nxt["id"]}/arrive',dict(version=route['version'],generation=route['generation'],captured_at=stamp())))
        data=dict(version=route['version'],generation=route['generation'],captured_at=stamp(),
            quantities={i['id']:i['quantity'] for i in route['items'] if i['pickup_id' if nxt['stop']['kind']=='PICKUP' else 'delivery_id']==nxt['stop_id']})
        if nxt['stop']['kind']=='DROPOFF':
            ev=check(post(mobile,BASE+f'/driver/stops/{nxt["stop_id"]}/evidence',dict(kind='SIGNATURE',media_type='image/png',captured_at=stamp(),content_base64=base64.b64encode(b'\x89PNG\r\n\x1a\nsig').decode())),201)
            data.update(recipient_name='Receiver',evidence_ids=[ev['id']])
        return check(post(mobile,BASE+f'/driver/routes/{route["id"]}/stops/{nxt["id"]}/complete',data))
    def complete_all(route):
        while any(v['status']!='COMPLETED' for v in route['stops']): route=complete_next(route)
        return check(post(mobile,BASE+f'/driver/routes/{route["id"]}/finish',dict(version=route['version'],generation=route['generation'])))
    with TestClient(app,headers=HEADERS) as alice, TestClient(app,headers=HEADERS) as bob:
        login(alice,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        login(bob,'customer','acme',other['email'],other['initial_password'])
        track=lambda c,o: check(c.get(BASE+f'/orders/{o["id"]}/tracking'))
        # Dedicated route: the shipper sees the live position for the whole trip.
        order=new_order(w)
        booked=track(alice,order); assert booked['stage']=='BOOKED' and booked['driver'] is None and booked['live'] is False
        route=assign(w,order)
        assigned=track(alice,order)
        assert assigned['stage']=='ASSIGNED' and assigned['driver']['first_name']=='Dana' and assigned['driver']['vehicle_type'] and assigned['dedicated'] is True
        assert assigned['location'] is None and [e['kind'] for e in assigned['events']]==['BOOKED','ASSIGNED']
        assert bob.get(BASE+f'/orders/{order["id"]}/tracking').status_code==404
        route=start(route); locate()
        moving=track(alice,order)
        assert moving['stage']=='TO_PICKUP' and moving['live'] is True and moving['location']['latitude']==49.2601 and moving['stops_before_next']==0
        assert moving['eta'] and all(stop['eta'] for stop in moving['stops'])
        complete_all(route)
        done=track(alice,order)
        assert done['stage']=='DELIVERED' and done['live'] is False and done['location'] is None
        assert {'STARTED','PICKUP_ARRIVED','PICKUP_COMPLETED','DROPOFF_ARRIVED','DROPOFF_COMPLETED'} <= {e['kind'] for e in done['events']}
        # Shared route: live position only while the driver's next visit is this shipper's stop.
        mine=new_order(w);theirs=check(post(client,BASE+'/orders',booking(other['id'],w['service'],w['type_id'])),201)
        route=assign(w,mine);route=assign(w,check(client.get(BASE+f'/orders/{theirs["id"]}')),route)
        route=start(route); locate()
        mine_stops={s['id'] for s in mine['facts']['stops']}
        seen=set()
        while any(v['status']!='COMPLETED' for v in route['stops']):
            nxt=next(v for v in route['stops'] if v['status']!='COMPLETED')
            view=track(alice,mine)
            assert view['dedicated'] is False and {s['id'] for s in view['stops']}==mine_stops
            if view['stage']!='DELIVERED':
                assert view['live']==(nxt['stop_id'] in mine_stops) and (view['location'] is not None)==view['live']
                assert check(client.get(BASE+f'/orders/{mine["id"]}/tracking'))['live'] is True
                seen.add(view['live'])
            route=complete_next(route)
        assert track(alice,mine)['stage']=='DELIVERED' and seen=={True,False}


def test_tracking_map_image_follows_tracking_visibility(setup, monkeypatch):
    from app.operations import tracking_map
    w=setup;client=w['client'];mobile=w['mobile']
    fetched=[]
    class Image:
        headers={'Content-Type':'image/png'}
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self,limit): return b'\x89PNG\r\n\x1a\nmap'
    def fake_open(url,timeout): fetched.append(url); return Image()
    monkeypatch.setattr(tracking_map.urllib.request,'urlopen',fake_open)
    monkeypatch.setattr(tracking_map,'order_road_path',lambda db,actor,booking: [[49.26,-123.11],[49.265,-123.11],[49.27,-123.11]])
    tracking_map.paths.rows.clear();tracking_map.images.rows.clear()
    order=new_order(w);waiting=new_order(w)
    image=lambda c,o: c.get(BASE+f'/orders/{o["id"]}/tracking/map')
    assert image(client,order).status_code==409 and not fetched
    route=assign(w,order)
    route=check(post(mobile,BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    check(post(mobile,BASE+'/driver/location',dict(duty_id=w['duty']['id'],captured_at=datetime.now(timezone.utc).isoformat(),latitude=49.2601,longitude=-123.1101,accuracy_m=8,location_permission='GRANTED')),201)
    monkeypatch.setenv('ROUTING_PROVIDER','google');monkeypatch.setenv('GOOGLE_ROUTES_API_KEY','server-key')
    monkeypatch.setenv('GOOGLE_MAPS_URL_SIGNING_SECRET',base64.urlsafe_b64encode(b'secret').decode())
    response=image(client,order)
    assert response.status_code==200 and response.headers['content-type']=='image/png' and response.content.startswith(b'\x89PNG')
    url=fetched[-1]
    assert url.startswith('https://maps.googleapis.com/maps/api/staticmap?') and 'key=server-key' in url and '&signature=' in url
    assert 'label:P|49.260000,-123.110000' in url and 'label:D|49.270000,-123.110000' in url and 'label:T|49.260100,-123.110100' in url and 'enc:' in url
    assert image(client,order).status_code==200 and len(fetched)==1
    with TestClient(app,headers=HEADERS) as alice, TestClient(app,headers=HEADERS) as bob:
        other=check(post(client,BASE+'/shippers',{**w['shipper_body'],'name':'Bob Shipper','company_name':'Other Co','email':'bob@example.com'}),201)
        login(alice,'customer','acme',w['shipper']['email'],w['shipper']['initial_password'])
        login(bob,'customer','acme',other['email'],other['initial_password'])
        assert image(alice,order).status_code==200
        assert image(alice,waiting).status_code==200 and 'label:T' not in fetched[-1]
        assert image(bob,order).status_code==404
