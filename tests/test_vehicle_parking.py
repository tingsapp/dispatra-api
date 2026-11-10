"""Vehicle parking origins, residential-address independence and legacy compatibility."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from conftest import owner_engine, post
from app.models import now
from app.dispatch import service
from app.operations import dispatch
from test_manual_operations import BASE, address, check, new_order, setup
from test_dispatch_agent import mode, suggest


def update_vehicle(w, data, version=None, key=None):
    return w['client'].put(BASE + f'/vehicles/{w["vehicle"]["id"]}',
        json={'version': version or w['vehicle']['version'], 'data': data},
        headers={'Idempotency-Key': key or str(uuid4())})


def test_registration_requires_parking_and_legacy_updates_preserve_it(setup):
    w = setup
    body = {**w['vehicle']['data'], 'unit_number': 'PARK2', 'plate': 'PARK2'}
    body.pop('parking_address')
    assert post(w['client'], BASE + '/vehicles', body).status_code == 422
    body['parking_address'] = {**address(), 'latitude': 91}
    assert post(w['client'], BASE + '/vehicles', body).status_code == 422
    body['parking_address'] = address(city='Burnaby', lat=49.25)
    created = check(post(w['client'], BASE + '/vehicles', body), 201)
    assert created['data']['parking_address']['city'] == 'Burnaby'
    original = w['vehicle']['data']['parking_address']
    legacy = {k: v for k, v in w['vehicle']['data'].items() if k != 'parking_address'}
    key = str(uuid4())
    saved = check(update_vehicle(w, legacy, key=key))
    assert saved['data']['parking_address'] == original
    assert check(update_vehicle(w, legacy, key=key)) == saved
    assert update_vehicle(w, {**legacy, 'parking_address': None}, saved['version']).status_code == 422
    assert update_vehicle(w, {**legacy, 'parking_address': address(city='Richmond')}).status_code == 409
    edited = check(update_vehicle(w, {**legacy, 'parking_address': address(city='Richmond')}, saved['version']))
    assert edited['data']['parking_address']['city'] == 'Richmond'


def test_manual_recommendation_and_assignment_start_at_vehicle_not_home(setup, monkeypatch):
    w = setup
    home = address(city='Victoria', lat=48.43, lng=-123.37)
    home['province'] = 'AB'  # Neither residential city nor province constrains work.
    check(w['client'].put(BASE + f'/drivers/{w["driver"]["id"]}',
        json={'version': w['driver']['version'], 'data': {**w['driver_body'], 'address': home}},
        headers={'Idempotency-Key': str(uuid4())}))
    order = new_order(w)
    decision = check(suggest(w['client'], order))
    candidate = decision['candidates'][0]
    assert candidate['metrics']['position'] == 'PARKING'
    assert candidate['metrics']['deadhead_km'] == 0
    assert any('vehicle parking address' in fact for fact in candidate['facts'])
    origins = []
    planner = dispatch.plan_route
    def capture(*args, **kwargs):
        origins.append(kwargs['origin'])
        return planner(*args, **kwargs)
    monkeypatch.setattr(dispatch, 'plan_route', capture)
    assigned = check(post(w['client'], BASE + f'/dispatch-decisions/{decision["id"]}/approve',
        {'version': decision['version'], 'driver_id': w['driver']['id']}))
    assert origins[-1] == w['vehicle']['data']['parking_address']
    assert assigned['route']['driver_id'] == w['driver']['id']
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}'))['pricing'] == order['pricing']


def test_manually_entered_parking_is_located_or_save_is_rejected(setup, monkeypatch):
    from app.intake import geocode
    w = setup
    parking = {**address(city='Burnaby'), 'latitude': None, 'longitude': None}
    payload = {**w['vehicle']['data'], 'parking_address': parking}
    monkeypatch.setattr(geocode, 'verify', lambda *_: None)
    assert update_vehicle(w, payload).status_code == 422
    monkeypatch.setattr(geocode, 'verify', lambda *_: {'latitude': 49.25, 'longitude': -122.95})
    saved = check(update_vehicle(w, payload))
    assert saved['data']['parking_address']['latitude'] == 49.25


def test_gps_takes_priority_then_stale_gps_returns_to_parking(setup):
    w = setup
    check(post(w['mobile'], BASE + '/driver/location', {
        'duty_id': w['duty']['id'], 'captured_at': now().isoformat(),
        'latitude': 49.27, 'longitude': -123.12, 'accuracy_m': 10, 'location_permission': 'GRANTED'}), 201)
    order = new_order(w)
    first = check(suggest(w['client'], order, refresh=True))['candidates'][0]
    assert first['metrics']['position'] == 'GPS' and first['metrics']['deadhead_km'] > 0
    with owner_engine.begin() as db:
        db.execute(text('UPDATE driver_locations SET captured_at=:old'), {'old': now() - timedelta(minutes=3)})
    second = check(suggest(w['client'], order, refresh=True))['candidates'][0]
    assert second['metrics']['position'] == 'PARKING' and second['metrics']['deadhead_km'] == 0


def test_legacy_vehicle_without_parking_is_readable_but_home_is_never_used(setup):
    w = setup
    with owner_engine.begin() as db:
        db.execute(text("UPDATE vehicles SET data=data-'parking_address' WHERE id=:id"), {'id': w['vehicle']['id']})
    assert check(w['client'].get(BASE + '/vehicles'))[0]['data']['parking_address'] is None
    order = new_order(w)
    decision = check(suggest(w['client'], order))
    assert decision['status'] == 'NO_CANDIDATE'
    assert 'parking address' in decision['excluded'][0]['reason']
    blocked = post(w['client'], BASE + f'/orders/{order["id"]}/assign', {
        'version': order['version'], 'driver_id': w['driver']['id'], 'vehicle_id': w['vehicle']['id'], 'planned_at': now().isoformat()})
    assert blocked.status_code == 409
    check(post(w['mobile'], BASE + '/driver/location', {
        'duty_id': w['duty']['id'], 'captured_at': now().isoformat(),
        'latitude': 49.26, 'longitude': -123.11, 'accuracy_m': 10, 'location_permission': 'GRANTED'}), 201)
    assert check(suggest(w['client'], order, refresh=True))['candidates'][0]['metrics']['position'] == 'GPS'


def test_auto_assignment_uses_parking_and_keeps_price(setup, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    w = setup
    mode(w['client'], 'AUTO')
    order = new_order(w)
    organization_id = service.auto_companies()[0]
    assert service.dispatch_pending(organization_id)['ASSIGNED'] == 1
    saved = check(w['client'].get(BASE + f'/orders/{order["id"]}'))
    assert saved['status'] == 'ASSIGNED' and saved['pricing'] == order['pricing']
    history = check(w['client'].get(BASE + f'/orders/{order["id"]}/dispatch-decisions'))
    assert history[0]['candidates'][0]['metrics']['position'] == 'PARKING'
