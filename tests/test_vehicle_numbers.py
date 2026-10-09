"""Unit-based vehicle IDs, tenant uniqueness and legacy migration preservation."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import company, login, owner_engine, post
from test_manual_operations import BASE, check, new_order, setup


def vehicle_body(w, unit='v12', plate='NEW123'):
    return {**w['vehicle']['data'], 'name': unit, 'unit_number': unit, 'plate': plate}


def test_vehicle_units_create_edit_and_replay_without_changing_identity(setup):
    w = setup
    key = str(uuid4())
    body = vehicle_body(w, ' v12 ')
    first = check(post(w['client'], BASE + '/vehicles', body, key), 201)
    assert first['number'] == 'DAV-V12' and first['data']['unit_number'] == 'V12'
    assert check(post(w['client'], BASE + '/vehicles', body, key), 201) == first
    duplicate = post(w['client'], BASE + '/vehicles', vehicle_body(w, 'V12', 'NEW456'))
    assert duplicate.status_code == 409 and 'Unit number already exists' in duplicate.json()['error']['message']
    edit_key = str(uuid4())
    edit = {'version': first['version'], 'data': vehicle_body(w, 'v13')}
    changed = check(w['client'].put(BASE + f'/vehicles/{first["id"]}', json=edit, headers={'Idempotency-Key': edit_key}))
    assert changed['id'] == first['id'] and changed['number'] == 'DAV-V13'
    assert changed['version'] == first['version'] + 1
    assert check(w['client'].put(BASE + f'/vehicles/{first["id"]}', json=edit, headers={'Idempotency-Key': edit_key})) == changed
    assert w['client'].put(BASE + f'/vehicles/{first["id"]}', json=edit, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    collision = {'version': changed['version'], 'data': vehicle_body(w, 'test van')}
    assert w['client'].put(BASE + f'/vehicles/{first["id"]}', json=collision, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    assert check(w['client'].get(BASE + f'/drivers/{w["driver"]["id"]}'))['vehicle_id'] == w['vehicle']['id']
    long_unit = 'V' * 50
    assert check(post(w['client'], BASE + '/vehicles', vehicle_body(w, long_unit, 'LONG123')), 201)['number'] == 'DAV-' + long_unit


def test_vehicle_unit_is_company_scoped_and_archived_units_remain_reserved(setup):
    w = setup
    first = check(post(w['client'], BASE + '/vehicles', vehicle_body(w)), 201)
    check(post(w['client'], BASE + f'/vehicles/{first["id"]}/archive', {'version': first['version']}))
    assert post(w['client'], BASE + '/vehicles', vehicle_body(w, ' V12 ', 'REUSE123')).status_code == 409
    login(w['client']); company(w['client'], 'other'); login(w['client'], 'dispatch', 'other', 'dispatcher')
    other_base = '/api/v1/companies/other'
    type_id = next(row['id'] for row in check(w['client'].get(other_base + '/catalog')) if row['code'] == 'veh_1_ton')
    other = check(post(w['client'], other_base + '/vehicles', {**vehicle_body(w), 'type_id': type_id}), 201)
    assert other['number'] == 'DOV-V12'
    assert w['client'].put(other_base + f'/vehicles/{first["id"]}', json={'version': first['version'], 'data': vehicle_body(w)}, headers={'Idempotency-Key': str(uuid4())}).status_code == 404


def test_vehicle_unit_database_index_blocks_duplicate_even_with_another_public_id(setup):
    with owner_engine.begin() as db:
        db.execute(text("SELECT set_config('app.platform', 'true', true)"))
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text('''INSERT INTO vehicles (id, organization_id, number, type_id, plate, province, active, data, version, created_at, updated_at)
                SELECT :id, organization_id, 'DAV-DIFFERENT', type_id, 'DUPLICATE', province, true,
                    jsonb_set(data, '{unit_number}', '" test van "'::jsonb), 1, now(), now()
                FROM vehicles WHERE id = :original'''), {'id': uuid4(), 'original': setup['vehicle']['id']})


def test_concurrent_vehicle_registrations_cannot_share_a_unit(setup):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda plate: post(setup['client'], BASE + '/vehicles', vehicle_body(setup, 'RACE12', plate)), ['RACE1', 'RACE2']))
    assert sorted(response.status_code for response in responses) == [201, 409]
    fleet = check(setup['client'].get(BASE + '/vehicles'))
    assert sum(row['data']['unit_number'] == 'RACE12' for row in fleet) == 1


def test_vehicle_number_retains_company_prefix_after_rename():
    from app.operations.identifiers import vehicle_number
    assert vehicle_number(' v12 ', 'New Name', 'new', 'DAV-3748') == 'DAV-V12'
    assert vehicle_number('12', 'Dispatra', 'dispatra') == 'DDV-12'


def migration():
    path = Path(__file__).parents[1] / 'migrations/versions/0027_vehicle_unit_numbers.py'
    spec = importlib.util.spec_from_file_location('vehicle_unit_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vehicle_number_migration_preserves_driver_links_and_commercial_history(setup):
    w = setup
    order = new_order(w)
    change = migration()
    with owner_engine.begin() as db:
        db.execute(text("SELECT set_config('app.platform', 'true', true)"))
        with Operations.context(MigrationContext.configure(db)):
            change.downgrade()
            db.execute(text("UPDATE vehicles SET number = 'DAV-3748', data = jsonb_set(data, '{unit_number}', '\" v12 \"'::jsonb) WHERE id = :id"), {'id': w['vehicle']['id']})
            change.upgrade()
    saved = check(w['client'].get(BASE + f'/vehicles/{w["vehicle"]["id"]}'))
    assert saved['number'] == 'DAV-V12' and saved['data']['unit_number'] == 'V12'
    assert saved['version'] == w['vehicle']['version'] + 1
    assert check(w['client'].get(BASE + f'/drivers/{w["driver"]["id"]}'))['vehicle_id'] == w['vehicle']['id']
    persisted = check(w['client'].get(BASE + f'/orders/{order["id"]}'))
    assert persisted['booking'] == order['booking'] and persisted['pricing'] == order['pricing']


def test_vehicle_number_migration_refuses_duplicate_legacy_units_without_renaming_them(setup):
    second = check(post(setup['client'], BASE + '/vehicles', vehicle_body(setup)), 201)
    change = migration()
    with owner_engine.begin() as db:
        db.execute(text("SELECT set_config('app.platform', 'true', true)"))
        with pytest.raises(RuntimeError, match='duplicate groups'), db.begin_nested():
            with Operations.context(MigrationContext.configure(db)):
                change.downgrade()
                db.execute(text("UPDATE vehicles SET data = jsonb_set(data, '{unit_number}', '\" test van \"'::jsonb) WHERE id = :id"), {'id': second['id']})
                change.upgrade()
    saved = check(setup['client'].get(BASE + f'/vehicles/{second["id"]}'))
    assert saved['number'] == 'DAV-V12' and saved['data']['unit_number'] == 'V12'
