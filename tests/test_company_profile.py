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


def test_operating_calendar_defaults_validate_and_sort():
    import pytest
    from pydantic import ValidationError
    from app.operations.schemas import SettingsData
    data = SettingsData()
    assert {(d.open, d.start, d.end) for d in data.work_hours} == {(True, '06:00', '19:00')} and data.holidays == []
    saved = SettingsData(holidays=[{'date': '2026-12-26', 'name': 'Boxing Day'}, {'date': '2026-12-25', 'name': 'Christmas Day'}])
    assert [h.name for h in saved.holidays] == ['Christmas Day', 'Boxing Day']
    hours = [d.model_dump() for d in data.work_hours]
    # A closed day may keep end-before-start times; an open day may not.
    assert SettingsData(work_hours=[*hours[:6], {**hours[6], 'open': False, 'start': '18:00', 'end': '09:00'}])
    for bad in [dict(work_hours=[{**hours[0], 'start': '17:00', 'end': '08:00'}, *hours[1:]]), dict(work_hours=hours[:6]),
                dict(work_hours=[hours[1], hours[0], *hours[2:]]), dict(work_hours=[{**hours[0], 'start': '8:00'}, *hours[1:]]),
                dict(holidays=[{'date': '2026-12-25', 'name': 'A'}, {'date': '2026-12-25', 'name': 'B'}]),
                dict(holidays=[{'date': '2026-12-25', 'name': ''}])]:
        with pytest.raises(ValidationError):
            SettingsData(**bad)


def test_operating_calendar_persists_and_survives_saves_that_omit_it(client):
    login(client); company(client); login(client, 'dispatch', 'acme', 'dispatcher')
    path = '/api/v1/companies/acme/settings'
    original = client.get(path).json()
    assert len(original['data']['work_hours']) == 7 and original['data']['holidays'] == []
    hours = [{**d, 'start': '07:30', 'end': '19:00'} if d['day'] == 'SAT' else d for d in original['data']['work_hours']]
    holidays = [{'date': '2026-12-25', 'name': 'Christmas Day'}]
    body = {'version': original['version'], 'data': {**original['data'], 'work_hours': hours, 'holidays': holidays}}
    saved = client.put(path, json=body, headers={'Idempotency-Key': 'operating-calendar-001'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['data']['work_hours'][5] == {'day': 'SAT', 'open': True, 'start': '07:30', 'end': '19:00'}
    legacy = {k: v for k, v in saved.json()['data'].items() if k not in {'work_hours', 'holidays'}}
    kept = client.put(path, json={'version': saved.json()['version'], 'data': legacy}, headers={'Idempotency-Key': 'operating-calendar-002'})
    assert kept.status_code == 200, kept.text
    assert kept.json()['data']['work_hours'] == hours and kept.json()['data']['holidays'] == holidays
