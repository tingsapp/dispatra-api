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
    driver_body = dict(name='Dana Driver',email='dana@example.com',phone='6045550102',address=address(),vehicle_id=vehicle['id'],employment='OWNER_OPERATOR',revenue_share_percent=60,fuel_surcharge_share_percent=100)
    driver = check(post(client,BASE+'/drivers',driver_body),201)
    with TestClient(app,headers=HEADERS) as mobile:
        login(mobile,'driver','acme','dana@example.com',driver['initial_password'])
        duty = check(post(mobile,BASE+'/driver/duty',{'location_permission':'GRANTED'}),201)
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


def test_manual_delivery_invoice_and_immutable_pricing(setup):
    w=setup;client=w['client']
    order=new_order(w)
    assert order['pricing']['total']=='128.18'
    route=assign(w,order)
    assert finish(w,route)['status']=='COMPLETED'
    done=check(client.get(BASE+f'/orders/{order["id"]}'))
    assert done['status']=='COMPLETED'
    # Completed earnings retain the original shares even if the driver's terms change.
    check(client.put(BASE+'/drivers/'+w['driver']['id'],json={'version':w['driver']['version'],'data':{**w['driver_body'],'revenue_share_percent':80}},headers={'Idempotency-Key':str(uuid4())}))
    settings=check(client.get(BASE+'/settings')); settings['data']['gst_percent']='20'
    check(client.put(BASE+'/settings',json={'version':settings['version'],'data':settings['data']},headers={'Idempotency-Key':str(uuid4())}))
    invoice=check(post(client,BASE+f'/orders/{order["id"]}/invoice',{'version':done['version']}),201)
    assert invoice['total']=='128.18' and invoice['snapshot']['pricing']['stage']=='FINAL'
    again=check(post(client,BASE+f'/orders/{order["id"]}/invoice',{'version':done['version']}),201)
    assert invoice['id']==again['id']
    assert check(client.get(BASE+f'/drivers/{w["driver"]["id"]}/activity'))['estimated_payout']=='84.08'
    assert 'Invoice' in client.get(BASE+f'/invoices/{invoice["id"]}/document').text
    report=check(client.get(BASE+'/analytics'))
    assert report['completed_orders']==1
    assert report['rows'][0]['shipper_name'] and report['rows'][0]['driver_name']
    assert report['rows'][0]['pod_verified'] is True
    assert report['rows'][0]['service_name'] and report['rows'][0]['vehicle_unit']
    with owner_engine.connect() as db:
        assert db.scalar(text('select count(*) from invoices'))==1
        assert db.scalar(text('select count(*) from pricing_revisions'))==1


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
    assert w['mobile'].get(BASE+'/invoices').status_code==403


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
    route=assign(w,two,route)
    assert len(route['stops'])==4
    assert all(original_ids.get(v['stop_id'],v['id'])==v['id'] for v in route['stops'])
    route=check(post(w['mobile'],BASE+f'/driver/routes/{route["id"]}/start',dict(version=route['version'],generation=route['generation'])))
    third=new_order(w)
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
    assert post(w['client'],BASE+f'/orders/{order["id"]}/invoice',{'version':3}).status_code==409


def test_duty_boundary_and_quote_review(setup):
    w=setup;mobile=w['mobile'];duty=w['duty']
    before=datetime.now(timezone.utc)
    check(post(mobile,BASE+'/driver/duty/'+duty['id']+'/end',dict(version=duty['version'],ended_at=before.isoformat())))
    data=dict(duty_id=duty['id'],captured_at=datetime.now(timezone.utc).isoformat(),latitude=49.2,longitude=-123.1,accuracy_m=5,location_permission='GRANTED')
    assert post(mobile,BASE+'/driver/location',data).status_code==409
    assert post(mobile,BASE+'/driver/location',{**data,'captured_at':before.isoformat()}).status_code==201
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
        assert set(preferences)=={'currency','time_zone','weight_unit','dimension_unit','distance_unit','gst_enabled','gst_percent','provincial_enabled','provincial_percent','fuel_enabled','fuel_percent'}
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
    invoice=check(post(client,BASE+f'/orders/{first["id"]}/invoice',{'version':done['version']}),201)
    assert invoice['snapshot']['pricing']['stage']=='FINAL'
