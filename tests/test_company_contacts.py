"""Owner contact edits share the existing Company Profile data and transaction/version boundary."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from conftest import company, login, post, owner_engine, PASSWORD

ROOT = '/api/v1/platform/organizations'
SETTINGS = '/api/v1/companies/acme/settings'
ADDRESS = {'text': '123 Main Street, Vancouver, BC V5Y 1V4', 'city': 'Vancouver',
           'province': 'BC', 'postal_code': 'V5Y 1V4', 'country': 'CA', 'latitude': 49.26, 'longitude': -123.11}


def detail(client, org):
    response = client.get(f"{ROOT}/{org['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def save(client, org, body, key=None):
    return client.patch(f"{ROOT}/{org['id']}/overview", json=body, headers={'Idempotency-Key': key or str(uuid4())})


def test_creation_stores_optional_contacts_in_shared_settings(client):
    login(client)
    response = post(client, ROOT, {'name': 'Acme', 'slug': 'acme', 'admin_login': 'dispatcher',
        'password': PASSWORD, 'phone': ' 6045550100 ', 'address': ' 123 Manual Street '})
    assert response.status_code == 201, response.text
    org = response.json()
    saved = detail(client, org)
    assert saved['phone'] == '6045550100' and saved['address'] == '123 Manual Street' and saved['settings_version'] == 1
    login(client, 'dispatch', 'acme', 'dispatcher')
    data = client.get(SETTINGS).json()['data']
    assert data['phone'] == saved['phone'] and data['address'] == saved['address']


@pytest.mark.parametrize('field,value', [('phone', '1' * 51), ('address', 'x' * 501)])
def test_creation_rejects_oversized_contacts_before_writing(client, field, value):
    login(client)
    response = post(client, ROOT, {'name': 'Acme', 'slug': 'acme', 'admin_login': 'dispatcher', 'password': PASSWORD, field: value})
    assert response.status_code == 422
    assert client.get(ROOT).json() == []


def test_owner_contact_save_replay_clear_and_dispatcher_draft_conflict(client):
    login(client); org = company(client)
    original = detail(client, org)
    login(client, 'dispatch', 'acme', 'dispatcher')
    profile = client.get(SETTINGS).json()
    login(client)
    body = {'version': 1, 'name': 'Saved company', 'active': True, 'settings_version': original['settings_version'],
            'phone': '6045550199', 'address': '456 Saved Street'}
    key = str(uuid4())
    response = save(client, org, body, key)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['company']['phone'] == body['phone'] and result['company']['address'] == body['address']
    assert result['company']['settings_version'] == 2
    assert save(client, org, body, key).json() == result
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.put(SETTINGS, json={'version': profile['version'], 'data': profile['data']}, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    saved = client.get(SETTINGS).json()
    assert saved['data']['phone'] == body['phone'] and saved['data']['address'] == body['address']
    assert saved['data']['fuel_percent'] == profile['data']['fuel_percent']
    login(client)
    cleared = save(client, org, {**body, 'version': 2, 'settings_version': 2, 'phone': '', 'address': ''})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['company']['phone'] == cleared.json()['company']['address'] == ''
    assert 'organization.updated' in [row['action'] for row in client.get(f"{ROOT}/{org['id']}/audit").json()]


def test_dispatcher_contact_change_blocks_a_stale_owner_draft_and_preserves_structured_address(client):
    login(client); org = company(client)
    initial = detail(client, org)
    login(client, 'dispatch', 'acme', 'dispatcher')
    profile = client.get(SETTINGS).json()
    profile['data'].update(phone='6045550177', address=ADDRESS)
    response = client.put(SETTINGS, json={'version': profile['version'], 'data': profile['data']}, headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    login(client)
    body = {'version': initial['version'], 'name': initial['name'], 'active': True,
        'phone': '6045550199', 'settings_version': initial['settings_version']}
    assert save(client, org, body).status_code == 409
    current = detail(client, org)
    assert current['phone'] == '6045550177' and current['address'] == ADDRESS['text']
    body.update(settings_version=current['settings_version'], address=current['address'])
    saved = save(client, org, body)
    assert saved.status_code == 200, saved.text
    login(client, 'dispatch', 'acme', 'dispatcher')
    assert client.get(SETTINGS).json()['data']['address'] == ADDRESS


def test_contact_edit_failure_rolls_back_every_field_and_requires_settings_version(client, monkeypatch):
    login(client); org = company(client)
    before = detail(client, org)
    body = {'version': before['version'], 'name': 'Unsaved company', 'active': False, 'phone': '6045550199', 'address': 'Unsaved Street'}
    assert save(client, org, body).status_code == 422
    body['settings_version'] = before['settings_version']
    def fail(*args): raise HTTPException(503, 'Access change failed.')
    monkeypatch.setattr('app.platform.service.apply_company_access', fail)
    assert save(client, org, body).status_code == 503
    assert detail(client, org) == before
    with owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM audit_events WHERE organization_id = :org AND action = 'organization.updated'"), {'org': org['id']}).scalar() == 0
