from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.operations.schemas import Booking, RateData, SettingsData, CatalogData, Discount
from app.operations.pricing import calculate
from app.operations.routing import optimize
from test_manual_operations import booking, address


def test_all_pricing_methods_tax_minimum_discount_and_frozen_context():
    data=Booking.model_validate(booking(uuid4(),uuid4(),uuid4()))
    def context(method, **kw):
        return {'card_id':str(uuid4()),'card_version':1,'card':RateData(name='Test',method=method,**kw).model_dump(mode='json'),
            'settings':SettingsData(fuel_enabled=False).model_dump(mode='json'),'service':CatalogData(name='Standard').model_dump(mode='json'),
            'vehicle':None,'accessorials':[],'discount':Discount().model_dump(mode='json')}
    fixed=context('FIXED',fixed_amount=100)
    assert calculate(data,fixed)['total']=='105.00'
    distance=context('BASE_PLUS_DISTANCE',base_fee=20,included_km=5,per_km='1.5')
    assert calculate(data.model_copy(update={'distance_km':Decimal(15)}),distance)['subtotal']=='35.00'
    with pytest.raises(HTTPException): calculate(data,distance)
    hourly=context('HOURLY',hourly_rate=60,minimum_minutes=120,increment_minutes=30)
    assert calculate(data.model_copy(update={'estimated_minutes':130}),hourly)['subtotal']=='150.00'
    zone=context('ZONE',zones=[{'id':'one','name':'Zone 1','postal_codes':['V5Y']}],weight_bands=[{'from_kg':0,'to_kg':99,'prices':{'one':20}}])
    assert calculate(data,zone)['subtotal']=='20.00'
    zone['card']['weight_bands'][0]['to_kg']='1'
    with pytest.raises(HTTPException): calculate(data,zone)
    imported=context('IMPORTED')
    assert calculate(data.model_copy(update={'imported_total':Decimal(105),'imported_tax':Decimal(5),'external_reference':'Legacy-1'}),imported)['total']=='105.00'
    fixed['card']['minimum_subtotal']='90'
    fixed['discount']={'kind':'PERCENT','value':'50'}
    fixed['settings'].update(provincial_enabled=True,provincial_percent='7')
    price=calculate(data,fixed)
    assert (price['subtotal'],price['tax'],price['total'])==('90.00','10.80','100.80')
    assert [line['label'] for line in price['lines'] if line['group']=='TAX']==['GST/HST','Provincial tax']


def test_package_handling_accessorials_follow_item_quantities(monkeypatch):
    from types import SimpleNamespace
    from app.operations import pricing
    org_id, service_id, card_id = uuid4(), uuid4(), uuid4()
    raw = booking(uuid4(), service_id, None, card_id)
    raw['items'][0].update(quantity=3, fragile=True, dangerous_goods=True)
    data = Booking.model_validate(raw)
    accessories = {
        code: SimpleNamespace(id=uuid4(), code=code, data=CatalogData(name=code, amount=amount).model_dump(mode='json'))
        for code, amount in [('FRAGILE', 15), ('DG', 25)]
    }
    card = SimpleNamespace(id=card_id, version=1, active=True, data=RateData(name='Fixed', method='FIXED', fixed_amount=100).model_dump(mode='json'))
    class Database:
        def scalar(self, statement):
            code = next((value for value in statement.compile().params.values() if value in accessories), None)
            return accessories.get(code)
    monkeypatch.setattr(pricing, 'settings', lambda *_: (None, SettingsData(fuel_enabled=False, gst_enabled=False)))
    monkeypatch.setattr(pricing, 'operational_shipper', lambda *_: None)
    monkeypatch.setattr(pricing, 'record', lambda *_: card)
    monkeypatch.setattr(pricing, 'catalog', lambda _db, _actor, _id, kind: next(
        (item for item in accessories.values() if item.id == _id), None) if kind == 'ACCESSORIAL'
        else SimpleNamespace(data=CatalogData(name='Standard').model_dump(mode='json')))
    context = pricing.price_context(Database(), SimpleNamespace(organization_id=org_id), data)
    assert {item['code']: item['quantity'] for item in context['accessorials']} == {'FRAGILE': '3', 'DG': '3'}
    selected = Booking.model_validate({**raw, 'accessorials': [
        {'id': str(accessories['FRAGILE'].id), 'quantity': 99}]})
    selected_context = pricing.price_context(Database(), SimpleNamespace(organization_id=org_id), selected)
    assert {item['code']: item['quantity'] for item in selected_context['accessorials']} == {'FRAGILE': '3', 'DG': '3'}
    price = calculate(data, context)
    assert price['subtotal'] == '220.00'
    accessory_lines = [(line['label'], line['amount']) for line in price['lines'] if line['group'] == 'ACCESSORIAL']
    assert accessory_lines == [('FRAGILE (3 packages)', '45.00'), ('DG (3 packages)', '75.00')], accessory_lines
    legacy = calculate(data, {key: value for key, value in context.items() if key != 'package_handling_unit_pricing'})
    assert [line['amount'] for line in legacy['lines'] if line['group'] == 'ACCESSORIAL'] == ['15.00', '25.00']


def test_route_intermediate_load_precedence_and_unknown_coordinates():
    first=booking(uuid4(),uuid4(),uuid4());second=booking(uuid4(),uuid4(),uuid4())
    for facts in [first,second]: facts['items'][0]['weight_kg']='80'
    stops=[s.model_dump(mode='json') for facts in [first,second] for s in Booking.model_validate(facts).stops]
    items=[i.model_dump(mode='json') for facts in [first,second] for i in Booking.model_validate(facts).items]
    vehicle={'payload_kg':'100','volume_m3':'10','length_cm':'100','width_cm':'100','height_cm':'100','pallet_capacity':2,'maximum_stops':4}
    plan=optimize(stops,items,vehicle,datetime.now(timezone.utc),None,480)
    assert all(Decimal(s['load_kg'])<=100 for s in plan['stops'])
    positions={s['stop_id']:i for i,s in enumerate(plan['stops'])}
    assert all(positions[item['pickup_id']]<positions[item['delivery_id']] for item in items)
    stops[0]['address']['latitude']=None
    with pytest.raises(HTTPException): optimize(stops,items,vehicle,datetime.now(timezone.utc),None,480)


def test_google_adapter_minimal_fields_and_incomplete_matrix(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.operations import travel
    calls=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,limit): return json.dumps([{'originIndex':0,'destinationIndex':1,'status':{},'condition':'ROUTE_EXISTS','duration':'120s'}]).encode()
    def open_request(request,timeout):
        calls.append(request)
        return Reply()
    monkeypatch.setenv('ROUTING_PROVIDER','google');monkeypatch.setenv('GOOGLE_ROUTES_API_KEY','unit-test-not-a-key')
    monkeypatch.setattr(travel.urllib.request,'urlopen',open_request)
    stops=[{'id':'a','address':address()},{'id':'b','address':address(lat=49.27)}]
    assert travel.matrix(None,SimpleNamespace(),stops)=={('a','b'):2.0}
    assert calls[0].get_header('X-goog-fieldmask')=='originIndex,destinationIndex,status,condition,duration'
    assert 'polyline' not in calls[0].data.decode()
    monkeypatch.setenv('ROUTING_PROVIDER','disabled')
    with pytest.raises(HTTPException): travel.matrix(None,SimpleNamespace(),stops)


def test_order_travel_returns_road_distance_and_minutes_for_distance_and_hourly_cards(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.operations import travel
    calls=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,limit): return json.dumps({'routes':[{'distanceMeters':12345,'duration':'1501s'}]}).encode()
    def open_request(request,timeout):
        calls.append(request); return Reply()
    monkeypatch.setenv('ROUTING_PROVIDER','google');monkeypatch.setenv('GOOGLE_ROUTES_API_KEY','unit-test-not-a-key')
    monkeypatch.setattr(travel.urllib.request,'urlopen',open_request)
    data=Booking.model_validate(booking(uuid4(),uuid4(),uuid4()))
    assert travel.order_travel(None,SimpleNamespace(),data)==(Decimal('12.35'),26)
    assert calls[0].get_header('X-goog-fieldmask')=='routes.distanceMeters,routes.duration'
    base={'card_id':str(uuid4()),'card_version':1,'settings':SettingsData(fuel_enabled=False,gst_enabled=False).model_dump(mode='json'),
        'service':CatalogData(name='Standard').model_dump(mode='json'),'vehicle':None,'accessorials':[],'discount':Discount().model_dump(mode='json')}
    distance={**base,'distance_km':'12.35','estimated_minutes':None,'card':RateData(name='Distance',method='BASE_PLUS_DISTANCE',base_fee=10,included_km=0,per_km=2).model_dump(mode='json')}
    assert calculate(data,distance)['subtotal']=='34.70'
    hourly={**base,'distance_km':None,'estimated_minutes':46,'card':RateData(name='Hourly',method='HOURLY',hourly_rate=60,minimum_minutes=30,increment_minutes=15).model_dump(mode='json')}
    assert calculate(data,hourly)['subtotal']=='60.00'
    monkeypatch.setenv('ROUTING_PROVIDER','demo')
    with pytest.raises(HTTPException): travel.order_travel(None,SimpleNamespace(),data)


def test_order_road_path_decodes_google_polyline(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.operations import travel
    calls=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        # Google's documented sample: (38.5,-120.2) -> (40.7,-120.95) -> (43.252,-126.453)
        def read(self,limit): return json.dumps({'routes':[{'polyline':{'encodedPolyline':'_p~iF~ps|U_ulLnnqC_mqNvxq`@'}}]}).encode()
    def open_request(request,timeout):
        calls.append(request); return Reply()
    monkeypatch.setenv('ROUTING_PROVIDER','google');monkeypatch.setenv('GOOGLE_ROUTES_API_KEY','unit-test-not-a-key')
    monkeypatch.setattr(travel.urllib.request,'urlopen',open_request)
    data=Booking.model_validate(booking(uuid4(),uuid4(),uuid4()))
    assert travel.order_road_path(None,SimpleNamespace(),data)==[[38.5,-120.2],[40.7,-120.95],[43.252,-126.453]]
    assert calls[0].get_header('X-goog-fieldmask')=='routes.polyline.encodedPolyline'
    monkeypatch.setenv('ROUTING_PROVIDER','demo')
    with pytest.raises(HTTPException): travel.order_road_path(None,SimpleNamespace(),data)


def test_zone_weight_uses_movement_totals_not_sum_of_package_maxima():
    raw=booking(uuid4(),uuid4(),uuid4())
    first=raw['items'][0]
    first.update(weight_kg='60',length_cm='10',width_cm='10',height_cm='10')
    raw['items'].append({**first,'id':str(uuid4()),'weight_kg':'1','length_cm':'100','width_cm':'100','height_cm':'40'})
    data=Booking.model_validate(raw)
    # Actual = 61kg, dimensional = 80.2kg. Summing package maxima incorrectly gives 140kg.
    context={'card_id':str(uuid4()),'card_version':1,'card':RateData(name='Zone',method='ZONE',
        zones=[{'id':'one','name':'Zone 1','postal_codes':['V5Y']}],
        weight_bands=[{'from_kg':0,'to_kg':99,'prices':{'one':20}}]).model_dump(mode='json'),
        'settings':SettingsData(fuel_enabled=False).model_dump(mode='json'),'service':CatalogData(name='Standard').model_dump(mode='json'),
        'vehicle':None,'accessorials':[],'discount':Discount().model_dump(mode='json')}
    assert calculate(data,context)['subtotal']=='20.00'
