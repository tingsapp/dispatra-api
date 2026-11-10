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


def test_review_reasons_keep_extracted_facts(monkeypatch):
    from app.intake import service
    value=extract.Extraction(is_order_request=True,summary='Date conflict',service=None,vehicle_type=None,scheduled_at=None,stops=[],items=[],reference=None,review_reasons=['Conflicting date'])
    _,draft,_=service._prepare(value,{'time_zone':'UTC','services':[],'shipper':None,'vehicle_types':[]})
    assert draft is not None and draft['service_id'] is None


def result(lat=49.26,postal='V5C 3Y5'):
    parts=[('street_number','456','456'),('route','Lougheed Highway','Lougheed Hwy'),('locality','Burnaby','Burnaby'),('administrative_area_level_1','British Columbia','BC'),('country','Canada','CA'),('postal_code',postal,postal)]
    return {'formatted_address':'456 Lougheed Highway, Burnaby, BC '+postal,'geometry':{'location_type':'ROOFTOP','location':{'lat':lat,'lng':-122.9}},'address_components':[{'types':[kind],'long_name':long,'short_name':short} for kind,long,short in parts]}


def test_geocode_accepts_one_precise_address_and_rejects_ambiguity_or_postal_conflicts(monkeypatch):
    monkeypatch.setattr(geocode,'provider',lambda:'google');monkeypatch.setenv('GOOGLE_GEOCODING_API_KEY','test')
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
