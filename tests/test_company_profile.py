import base64
from conftest import company, login, post


def test_profile_settings_persist_and_enforce_versions(client):
    login(client); company(client); login(client, 'dispatch', 'acme', 'dispatcher')
    path = '/api/v1/companies/acme/settings'
    original = client.get(path).json()
    png = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'logo' * 800).decode()
    body = {'version': original['version'], 'data': {**original['data'],
        'company_name': 'Saved company', 'contact_name': 'Saved contact', 'phone': '6045550100',
        'address': 'Manual address without geocoding', 'logo_url': png,
        'gst_percent': '7.25', 'provincial_enabled': True, 'provincial_percent': '3.5',
        'weight_unit': 'kg', 'dimension_unit': 'cm', 'time_zone': 'America/Toronto'}}
    response = client.put(path, json=body, headers={'Idempotency-Key': 'company-profile-save-001'})
    assert response.status_code == 200, response.text
    saved = client.get(path).json()
    assert saved['data'] == body['data']
    assert client.get('/api/v1/auth/me').json()['organization']['name'] == 'Saved company'
    # Replays return the same result; another stale save cannot overwrite it.
    assert client.put(path, json=body, headers={'Idempotency-Key': 'company-profile-save-001'}).json() == response.json()
    assert client.put(path, json=body, headers={'Idempotency-Key': 'company-profile-save-002'}).status_code == 409
    bad = {**body, 'version': saved['version'], 'data': {**saved['data'], 'logo_url': 'data:image/svg+xml,<svg/>'}}
    assert client.put(path, json=bad, headers={'Idempotency-Key': 'company-profile-save-003'}).status_code == 422
    assert client.get('/api/v1/companies/another/settings').status_code == 404


def test_profile_rejects_invalid_logo_and_keeps_operational_addresses_structured():
    import pytest
    from pydantic import ValidationError
    from app.operations.schemas import SettingsData, ShipperData
    with pytest.raises(ValidationError):
        SettingsData(logo_url='data:image/png;base64,not-base64')
    with pytest.raises(ValidationError):
        SettingsData(logo_url='data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 205000).decode())
    with pytest.raises(ValidationError):
        ShipperData(name='Shipper', kind='INDIVIDUAL', email='shipper@example.com', warehouse='Just text')
