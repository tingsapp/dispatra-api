"""Representative outside profiles initialize presets without overwriting fleet facts."""
from decimal import Decimal
from uuid import uuid4

from conftest import login, company, post
from test_manual_operations import check, BASE
from app.operations.schemas import CatalogData
from app.operations.seeding import SEED


def test_new_company_has_complete_outside_profiles_and_cargo_remains_separate(client):
    login(client)
    company(client)
    login(client, 'dispatch', 'acme', 'dispatcher')
    rows = check(client.get(BASE+'/catalog'))
    types = [r for r in rows if r['kind'] == 'VEHICLE_TYPE']
    assert len([r for r in types if r['active']]) == 9
    for source in SEED['catalogue']['vehicles']:
        row = next(r for r in types if r['code'] == source.get('code', source['id']))
        data = CatalogData.model_validate(row['data'])
        assert data.road_profile is not None
        profile = data.road_profile
        assert profile.gross_weight_kg > data.payload_kg
        assert profile.axle_weight_kg is None  # No actual axle layout has been supplied.
        for axis in ('length', 'width', 'height'):
            assert getattr(profile, axis+'_m')*100 >= getattr(data, axis+'_cm')
        assert data.payload_kg == Decimal(str(source['payloadCapacityKg']))
        assert data.amount == Decimal(str(source['baseSurcharge']))
    cargo = next(r for r in types if r['code'] == 'veh_1_ton')
    assert cargo['data']['road_profile']['length_m'] == '6.7056'
    assert cargo['data']['road_profile']['gross_weight_kg'] == 4990
    # The regular edit form accepts a seeded profile without filling missing fields.
    saved = check(client.put(BASE+f"/catalog/{cargo['id']}", json={
        'version': cargo['version'], 'data': cargo['data'],
    }, headers={'Idempotency-Key': str(uuid4())}))
    assert saved['data']['road_profile'] == cargo['data']['road_profile']
    assert all(r['data']['road_profile'] is None for r in rows if r['kind'] != 'VEHICLE_TYPE')


def test_seed_retry_preserves_custom_profile_cargo_price_and_versions(client):
    login(client)
    company(client)
    login(client, 'dispatch', 'acme', 'dispatcher')
    row = next(r for r in check(client.get(BASE+'/catalog')) if r['code'] == 'veh_1_ton')
    values = {**row['data'], 'name': 'Measured fleet van', 'amount': '12.34', 'payload_kg': '1400',
              'road_profile': {**row['data']['road_profile'], 'height_m': '3.1', 'axle_weight_kg': 2500}}
    saved = check(client.put(BASE+f"/catalog/{row['id']}", json={
        'version': row['version'], 'data': values,
    }, headers={'Idempotency-Key': str(uuid4())}))
    before = check(client.get(BASE+'/catalog'))
    assert check(post(client, BASE+'/pricing/seed', {}))['created'] == 0
    assert check(post(client, BASE+'/pricing/seed', {}))['created'] == 0
    assert check(client.get(BASE+'/catalog')) == before
    assert next(r for r in before if r['id'] == saved['id']) == saved
