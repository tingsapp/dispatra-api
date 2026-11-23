import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
import pytest
from app.intake import booking, extract, geocode


def test_books_example_uses_company_units_and_verified_partial_address():
    service, liftgate = str(uuid4()),str(uuid4())
    ctx={'time_zone':'America/Vancouver','dimension_unit':'in','weight_unit':'lb','services':[{'id':service,'code':'DIRECT','name':'Direct Hotshot'}],
        'accessorials':[{'id':liftgate,'code':'LIFTGATE','name':'Liftgate'}],'vehicle_types':[],
        'shipper':{'id':str(uuid4()),'warehouse':{'text':'100 Warehouse St, Vancouver, BC V5Y 1V4','city':'Vancouver','province':'BC','postal_code':'V5Y 1V4','latitude':49.2,'longitude':-123.1}}}
    item=extract.Extraction.model_validate({'is_order_request':True,'summary':'Two pallets of books, urgent direct delivery with liftgate.',
        'service':'DIRECT','vehicle_type':None,'accessorials':['LIFTGATE'],'scheduled_at':'2030-11-07T09:00:00-08:00','reference':None,
        'stops':[{'kind':'PICKUP','use_shipper_warehouse':True,'address':None,'contact_name':None,'phone':None,'instructions':None,'window_start':None,'window_end':None},
            {'kind':'DROPOFF','use_shipper_warehouse':False,'address':{'street':'456 Lougheed hiway','city':'Burnaby','province':None,'postal_code':None},'contact_name':None,'phone':None,'instructions':'Liftgate required','window_start':None,'window_end':None}],
        'items':[{'pickup_index':0,'delivery_index':1,'quantity':2,'weight_each':200,'weight_unit':'lb','length':48,'width':48,'height':48,'dimension_unit':None,'pallets_each':1,'fragile':False,'dangerous_goods':False,'description':'Books'}]})
    calls=[]
    def verify(text,postal):
        calls.append((text,postal))
        return {'text':'456 Lougheed Highway, Burnaby, BC V5C 3Y5, Canada','city':'Burnaby','province':'BC','postal_code':'V5C 3Y5','latitude':49.26,'longitude':-122.9}
    draft,missing=booking.build(item,ctx,verify)
    assert not missing
    assert len(calls)==1 and calls[0][1]==''
    assert draft['items'][0]['weight_kg']=='90.72'
    assert [draft['items'][0][key] for key in ('length_cm','width_cm','height_cm')]==['121.92']*3
    assert draft['service_id']==service and draft['accessorials']==[{'id':liftgate,'quantity':'1'}]
    # A different city is not silently substituted for the supplied city.
    _,missing=booking.build(item,ctx,lambda *_:{**verify('', ''),'city':'Vancouver'})
    assert missing


def test_package_type_is_inferred_from_the_catalogue_and_never_overrides_email_measurements():
    parcel, pallet = str(uuid4()), str(uuid4())
    types=[{'id':parcel,'code':'PARCEL','name':'Parcel','description':'Boxes, bags and other regular packages.'},
        {'id':pallet,'code':'PALLET','name':'Pallet','description':'Goods stacked on a standard 48 x 40 in pallet.'}]
    ctx={'time_zone':'America/Vancouver','dimension_unit':'in','weight_unit':'lb','services':[],'vehicle_types':[],'package_types':types,
        'shipper':{'id':str(uuid4()),'warehouse':{'text':'100 Warehouse St, Vancouver, BC V5Y 1V4','city':'Vancouver','province':'BC','postal_code':'V5Y 1V4'}}}
    def books(**changes):
        return extract.Extraction.model_validate({'is_order_request':True,'summary':'One pallet of books.','service':None,'vehicle_type':None,'scheduled_at':None,'reference':None,
            'stops':[{'kind':'PICKUP','use_shipper_warehouse':True,'address':None,'contact_name':None,'phone':None,'instructions':None,'window_start':None,'window_end':None},
                {'kind':'DROPOFF','use_shipper_warehouse':False,'address':None,'contact_name':None,'phone':None,'instructions':None,'window_start':None,'window_end':None}],
            'items':[{'pickup_index':0,'delivery_index':1,'quantity':1,'weight_each':200,'weight_unit':'lb','length':48,'width':48,'height':48,'dimension_unit':None,
                'pallets_each':1,'fragile':False,'dangerous_goods':False,'description':'Books',**changes}]})
    def item(value): return booking.build(value,ctx,lambda *_:None)[0]['items'][0]
    chosen=item(books(package_type='PALLET'))
    assert chosen['package_type_id']==pallet
    assert (chosen['weight_kg'],chosen['length_cm'],chosen['width_cm'],chosen['height_cm'],chosen['pallets'])==('90.72','121.92','121.92','121.92',1)
    # Palletized cargo with no extracted type still becomes the company's Pallet type.
    assert item(books())['package_type_id']==pallet
    assert item(books(package_type='Parcel',pallets_each=0))['package_type_id']==parcel
    # Choosing Pallet counts one pallet per piece, as the order form does.
    assert item(books(package_type='PALLET',pallets_each=0))['pallets']==1
    assert item(books(package_type='CRATE',pallets_each=0))['package_type_id'] is None
    assert booking.build(books(),{**ctx,'package_types':[]},lambda *_:None)[0]['items'][0]['package_type_id'] is None
    # When the guess fails, the company Default applies; a palletized item still prefers Pallet.
    defaulted={**ctx,'default_package_type_id':parcel}
    assert booking.build(books(package_type='CRATE',pallets_each=0),defaulted,lambda *_:None)[0]['items'][0]['package_type_id']==parcel
    assert booking.build(books(package_type=None),defaulted,lambda *_:None)[0]['items'][0]['package_type_id']==pallet
    assert booking.build(books(package_type='CRATE',pallets_each=0),{**defaulted,'default_package_type_id':str(uuid4())},lambda *_:None)[0]['items'][0]['package_type_id'] is None
    prompt=json.loads(extract.prompt({'subject':'','body':'','received_at':'2026-11-13T10:00:00+00:00'},{**ctx,'shipper':None}))
    assert prompt['package_types']==types
    assert 'package_type' in extract.INSTRUCTIONS


def test_review_reasons_keep_extracted_facts(monkeypatch):
    from app.intake import service
    value=extract.Extraction(is_order_request=True,summary='Date conflict',service=None,vehicle_type=None,scheduled_at=None,stops=[],items=[],reference=None,review_reasons=['Conflicting date'])
    _,draft,_=service._prepare(value,{'time_zone':'UTC','services':[],'shipper':None,'vehicle_types':[]})
    assert draft is not None and draft['service_id'] is None


def result(lat=49.26,postal='V5C 3Y5'):
    parts=[('street_number','456','456'),('route','Lougheed Highway','Lougheed Hwy'),('locality','Burnaby','Burnaby'),('administrative_area_level_1','British Columbia','BC'),('country','Canada','CA'),('postal_code',postal,postal)]
    return {'formatted_address':'456 Lougheed Highway, Burnaby, BC '+postal,'geometry':{'location_type':'ROOFTOP','location':{'lat':lat,'lng':-122.9}},'address_components':[{'types':[kind],'long_name':long,'short_name':short} for kind,long,short in parts]}


def test_geocode_accepts_one_precise_address_and_rejects_ambiguity_or_postal_conflicts(monkeypatch):
    monkeypatch.setenv('GOOGLE_GEOCODING_API_KEY','test')
    monkeypatch.delenv('GOOGLE_ROUTES_API_KEY', raising=False)
    payload={'status':'OK','results':[result()]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(payload).encode()
    monkeypatch.setattr(geocode.urllib.request,'urlopen',lambda *args,**kw:Response())
    assert geocode.verify('456 Lougheed hiway, Burnaby','')['province']=='BC'
    assert geocode.verify('456 Lougheed hiway, Burnaby','V6C 1W6') is None
    assert geocode.verify('999 Lougheed Highway, Burnaby','') is None
    payload['results'].append(result(49.3))
    assert geocode.verify('456 Lougheed hiway, Burnaby','') is None
    payload['results']=[result()];payload['results'][0]['geometry']['location_type']='GEOMETRIC_CENTER'
    assert geocode.verify('456 Lougheed hiway, Burnaby','') is None
    payload['results']=[result()];payload['results'][0]['partial_match']=True
    assert geocode.verify('456 Lougheed hiway, Burnaby','') is None


def test_places_fallback_resolves_a_complete_address_with_the_routes_credential(monkeypatch):
    monkeypatch.delenv('GOOGLE_GEOCODING_API_KEY', raising=False)
    monkeypatch.setenv('GOOGLE_ROUTES_API_KEY', 'routes-test')
    payload = {'places': [{'formattedAddress': '4567 Lougheed Highway, Burnaby, BC V5C 3Z6, Canada',
        'location': {'latitude': 49.267, 'longitude': -123.001}, 'addressComponents': [
            {'types': ['street_number'], 'longText': '4567', 'shortText': '4567'},
            {'types': ['route'], 'longText': 'Lougheed Highway', 'shortText': 'Lougheed Highway'},
            {'types': ['locality'], 'longText': 'Burnaby', 'shortText': 'Burnaby'},
            {'types': ['administrative_area_level_1'], 'longText': 'British Columbia', 'shortText': 'BC'},
            {'types': ['country'], 'longText': 'Canada', 'shortText': 'CA'},
            {'types': ['postal_code'], 'longText': 'V5C 3Z6', 'shortText': 'V5C 3Z6'}]}]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(payload).encode()
    requests = []
    monkeypatch.setattr(geocode.urllib.request, 'urlopen', lambda request, **kwargs: requests.append(request) or Response())
    resolved = geocode.verify('4567 Lougheed Highway, Burnaby, BC V5C 3Z6, Canada', 'V5C 3Z6')
    assert resolved == {'text': '4567 Lougheed Highway, Burnaby, BC V5C 3Z6, Canada', 'latitude': 49.267, 'longitude': -123.001,
        'city': 'Burnaby', 'province': 'BC', 'postal_code': 'V5C 3Z6', 'country': 'CA'}
    assert requests[0].full_url == 'https://places.googleapis.com/v1/places:searchText'
    assert json.loads(requests[0].data)['regionCode'] == 'CA'


def test_places_fallback_resolves_a_us_address_and_its_zip_plus_four(monkeypatch):
    monkeypatch.delenv('GOOGLE_GEOCODING_API_KEY', raising=False)
    monkeypatch.setenv('GOOGLE_ROUTES_API_KEY', 'routes-test')
    payload = {'places': [{'formattedAddress': '400 Broad St, Seattle, WA 98109, USA',
        'location': {'latitude': 47.6205, 'longitude': -122.3493}, 'addressComponents': [
            {'types': ['street_number'], 'longText': '400', 'shortText': '400'},
            {'types': ['locality'], 'longText': 'Seattle', 'shortText': 'Seattle'},
            {'types': ['administrative_area_level_1'], 'longText': 'Washington', 'shortText': 'WA'},
            {'types': ['country'], 'longText': 'United States', 'shortText': 'US'},
            {'types': ['postal_code'], 'longText': '98109', 'shortText': '98109'}]}]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(payload).encode()
    requests = []
    monkeypatch.setattr(geocode.urllib.request, 'urlopen', lambda request, **kwargs: requests.append(request) or Response())
    resolved = geocode.verify('400 Broad St, Seattle, WA 98109-4607', '98109-4607')
    assert resolved == {'text': '400 Broad St, Seattle, WA 98109, USA', 'latitude': 47.6205, 'longitude': -122.3493,
        'city': 'Seattle', 'province': 'WA', 'postal_code': '98109', 'country': 'US'}
    assert json.loads(requests[0].data)['regionCode'] == 'US'


def test_geocoding_without_a_postal_code_keeps_only_canada_or_us_matches(monkeypatch):
    monkeypatch.setenv('GOOGLE_GEOCODING_API_KEY', 'geo-test')
    def result(country, postal):
        return {'formatted_address': f'10 Main St, Springfield, XX {postal}', 'geometry': {'location_type': 'ROOFTOP', 'location': {'lat': 1, 'lng': 2}},
            'address_components': [{'types': ['street_number'], 'long_name': '10', 'short_name': '10'},
                {'types': ['locality'], 'long_name': 'Springfield', 'short_name': 'Springfield'},
                {'types': ['administrative_area_level_1'], 'long_name': 'Illinois', 'short_name': 'IL'},
                {'types': ['country'], 'long_name': country, 'short_name': country},
                {'types': ['postal_code'], 'long_name': postal, 'short_name': postal}]}
    payload = {'status': 'OK', 'results': [result('US', '62701'), result('MX', '01000')]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(payload).encode()
    urls = []
    monkeypatch.setattr(geocode.urllib.request, 'urlopen', lambda url, **kwargs: urls.append(url) or Response())
    assert geocode.verify('10 Main St, Springfield, IL', '')['country'] == 'US'
    assert 'components=' not in urls[0]


@pytest.mark.parametrize('supplied,expected', [('Bunaby', True), ('Buranby', True), ('burnaby', True),
    ('Vancouver', False), ('North Burnaby', False), ('Surrey', False), ('', False)])
def test_city_typo_requires_a_close_match(supplied, expected):
    assert geocode.same_city(supplied, 'Burnaby') is expected


def test_local_email_times_use_timezone_rules_and_explicit_offsets_stay_explicit():
    zone = ZoneInfo('America/Vancouver')
    for clock in ('14:00:00', '17:00:00'):
        local = '2026-11-10T' + clock
        value = booking.moment(local, zone)
        assert datetime.fromisoformat(value).astimezone(zone).strftime('%H:%M:%S') == clock
        assert value == datetime.fromisoformat(local).replace(tzinfo=zone).isoformat()
    assert booking.moment('2026-11-10T14:00:00-05:00', zone) == '2026-11-10T14:00:00-05:00'


def test_prompt_supplies_company_local_received_date_and_service_meaning():
    context = {'time_zone': 'America/Vancouver', 'shipper': None, 'vehicle_types': [],
        'services': [{'code': 'SAME_DAY', 'name': 'Same-Day Standard', 'description': 'Standard delivery by 17:00.'}]}
    value = json.loads(extract.prompt({'received_at': '2026-11-10T01:00:00+00:00', 'subject': 'New order', 'body': 'Tomorrow at 2pm'}, context))
    assert value['received_at_company_local'].startswith('2026-11-09T')
    assert value['services'][0]['description'] == 'Standard delivery by 17:00.'
    assert 'Cargo Van as its standard default' in extract.INSTRUCTIONS
    assert 'EVERY requested additional service' in extract.INSTRUCTIONS
    assert 'dangerous_goods true' in extract.INSTRUCTIONS


def test_load_size_is_ftl_only_when_asked_otherwise_small_parcel_or_ltl_from_the_cargo():
    service, van, trailer = str(uuid4()), str(uuid4()), str(uuid4())
    ctx={'time_zone':'America/Vancouver','dimension_unit':'in','weight_unit':'lb','services':[{'id':service,'code':'SAME_DAY','name':'Same-Day Standard'}],
        'vehicle_types':[{'id':van,'code':'veh_1_ton','name':'Cargo Van','load_sizes':['LTL']},{'id':trailer,'code':'veh_trailer','name':'Dry Van Trailer','load_sizes':['FTL']}],
        'shipper':{'id':str(uuid4()),'warehouse':{'text':'100 Warehouse St, Vancouver, BC V5Y 1V4','city':'Vancouver','province':'BC','postal_code':'V5Y 1V4','latitude':49.2,'longitude':-123.1}}}
    def order(**changes):
        item={'pickup_index':0,'delivery_index':1,'quantity':2,'weight_each':20,'weight_unit':'lb','length':12,'width':10,'height':8,'dimension_unit':None,
            'pallets_each':0,'fragile':False,'dangerous_goods':False,'description':'Boxes',**changes.pop('item',{})}
        return extract.Extraction.model_validate({'is_order_request':True,'summary':'Boxes.','service':'SAME_DAY','vehicle_type':None,'scheduled_at':'2030-11-07T09:00:00','reference':None,
            'stops':[{'kind':'PICKUP','use_shipper_warehouse':True,'address':None,'contact_name':None,'phone':None,'instructions':None,'window_start':None,'window_end':None},
                {'kind':'DROPOFF','use_shipper_warehouse':False,'address':{'street':'500 Granville Street','city':'Vancouver','province':'BC','postal_code':'V6C 1W6'},
                    'contact_name':None,'phone':None,'instructions':None,'window_start':None,'window_end':None}],
            'items':[item],**changes})
    def build(value): return booking.build(value,ctx,lambda *_:None)
    # Two 20 lb boxes are a small parcel, which books no vehicle type.
    draft,_=build(order())
    assert (draft['load_size'],draft['service_id'],draft['vehicle_type_id'])==('SMALL_PARCEL',service,None)
    # Pallets, a heavy piece, a heavy total, an unknown weight or a requested vehicle type are less than truckload.
    for value in (order(item={'pallets_each':1}),order(item={'weight_each':80}),order(item={'quantity':4,'weight_each':40}),order(item={'weight_each':None}),order(vehicle_type='Cargo Van')):
        draft,_=build(value)
        assert (draft['load_size'],draft['vehicle_type_id'])==('LTL',van)
    # Saved extractions from before the field read as not a full truckload.
    assert extract.Extraction.model_validate(order().model_dump(exclude={'full_truckload'})).full_truckload is False
    # A full truckload takes a type offered for FTL, no service level, and needs no package weight or size.
    draft,missing=build(order(full_truckload=True,service='DIRECT',item={'weight_each':None,'length':None,'width':None,'height':None}))
    assert (draft['load_size'],draft['service_id'],draft['vehicle_type_id'])==('FTL',None,trailer)
    assert not missing
