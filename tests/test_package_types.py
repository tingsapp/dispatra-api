"""Company Package Types describe order items without changing price."""
from uuid import uuid4

from conftest import post
from test_manual_operations import BASE, check, booking, setup  # noqa: F401 - pytest fixture


def with_type(body, identity):
    body['items'][0]['package_type_id'] = identity
    return body


def test_new_company_has_default_package_types_that_items_reference_without_repricing(setup):
    w = setup
    types = {row['code']: row for row in w['catalog'] if row['kind'] == 'PACKAGE_TYPE'}
    assert set(types) == {'ENVELOPE', 'PARCEL', 'PALLET', 'OVERSIZED'}
    envelope = types['ENVELOPE']['data']
    assert (envelope['name'], envelope['default_weight_kg'], envelope['length_cm']) == ('Envelope', '0.5', '32')
    assert types['PARCEL']['data']['default_weight_kg'] is None
    plain = check(post(w['client'], BASE+'/orders', booking(w['shipper']['id'], w['service'], w['type_id'])), 201)
    typed = check(post(w['client'], BASE+'/orders', with_type(booking(w['shipper']['id'], w['service'], w['type_id']), types['ENVELOPE']['id'])), 201)
    assert typed['facts']['items'][0]['package_type_id'] == types['ENVELOPE']['id']
    assert plain['facts']['items'][0]['package_type_id'] is None
    assert typed['pricing']['total'] == plain['pricing']['total']


def test_only_active_company_package_types_are_accepted_and_edits_keep_a_deleted_one(setup):
    w = setup
    client = w['client']
    created = check(post(client, BASE+'/catalog', dict(kind='PACKAGE_TYPE', code='TUBE', data=dict(name='Tube', description='Drawings', length_cm='90', width_cm='10', height_cm='10'))), 201)
    order = check(post(client, BASE+'/orders', with_type(booking(w['shipper']['id'], w['service'], w['type_id']), created['id'])), 201)
    assert post(client, BASE+'/orders', with_type(booking(w['shipper']['id'], w['service'], w['type_id']), w['service'])).status_code == 422
    assert post(client, BASE+'/orders', with_type(booking(w['shipper']['id'], w['service'], w['type_id']), str(uuid4()))).status_code == 422
    check(client.request('DELETE', BASE+f"/catalog/{created['id']}", json=dict(version=created['version']), headers={'Idempotency-Key': str(uuid4())}))
    assert post(client, BASE+'/orders', with_type(booking(w['shipper']['id'], w['service'], w['type_id']), created['id'])).status_code == 422
    # The saved Order may be edited without replacing a type deleted after booking.
    facts = {**order['facts'], 'internal_notes': 'Kept tube'}
    edited = check(client.put(BASE+f"/orders/{order['id']}", json=dict(version=order['version'], booking=facts), headers={'Idempotency-Key': str(uuid4())}))
    assert edited['facts']['items'][0]['package_type_id'] == created['id']
