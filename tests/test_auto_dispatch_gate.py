"""One tenant-scoped journey verifies AUTO activation without repeating expensive schema resets."""
from uuid import uuid4
from sqlalchemy import text
from conftest import login, company, owner_engine
from test_connectors import configured, connect_truckmate, connection
from app.connectors import worker

BASE = '/api/v1/companies/acme/settings'


def save(client, saved, **changes):
    return client.put(BASE, json={'version': saved['version'], 'data': {**saved['data'], **changes}},
                      headers={'Idempotency-Key': str(uuid4())})


def test_external_auto_activation_connection_states_tenancy_and_native_modes(client, configured, monkeypatch):
    saved = client.get(BASE).json()
    response = save(client, saved, tms_workspace=True, dispatch_mode='MANUAL')
    assert response.status_code == 200, response.text
    saved = response.json()
    response = save(client, saved, dispatch_mode='AUTO')
    assert response.status_code == 409
    assert 'no TMS connector is connected' in response.json()['error']['message']
    assert client.get(BASE).json() == saved
    # An older client cannot bypass the effective saved External TMS preference.
    legacy = {**saved, 'data': {k: v for k, v in saved['data'].items() if k != 'tms_workspace'}}
    assert save(client, legacy, dispatch_mode='AUTO').status_code == 409
    connect_truckmate(client)
    org, identity = connection()

    def status(value):
        with owner_engine.begin() as db:
            db.execute(text('UPDATE connector_connections SET status=:status WHERE id=:id'), {'status': value, 'id': identity})

    for value in ['CONNECTING', 'SYNCING']:
        status(value)
        assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    status('CONNECTING')
    assert worker.sync(org, identity)
    for value in ['CONNECTED', 'SYNCING']:
        status(value)
        enabled = save(client, saved, dispatch_mode='AUTO')
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()['data']['dispatch_mode'] == 'AUTO'
        disabled = save(client, enabled.json(), dispatch_mode='MANUAL')
        assert disabled.status_code == 200
        saved = disabled.json()
    for value in ['RECONNECT_REQUIRED', 'SYNC_FAILED', 'DISCONNECTED']:
        status(value)
        assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    status('CONNECTED')
    with owner_engine.begin() as db:
        db.execute(text('UPDATE connector_connections SET paused=true WHERE id=:id'), {'id': identity})
    assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    with owner_engine.begin() as db:
        db.execute(text('UPDATE connector_connections SET paused=false WHERE id=:id'), {'id': identity})
        db.execute(text("UPDATE connector_providers SET enabled=false WHERE provider='truckmate'"))
    assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    with owner_engine.begin() as db:
        db.execute(text("UPDATE connector_providers SET enabled=true WHERE provider='truckmate'"))
    monkeypatch.delenv('TMS_CONNECTOR_PROFILES_FILE')
    assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    monkeypatch.setenv('TMS_CONNECTOR_PROFILES_FILE', str(configured['path']))

    # Another company's verified connector cannot authorize this company.
    login(client)
    company(client, 'other')
    login(client, 'dispatch', 'other', 'dispatcher')
    path = '/api/v1/companies/other/settings'
    other = client.get(path).json()
    assert client.put(path, json={'version': other['version'], 'data': {**other['data'], 'tms_workspace': True, 'dispatch_mode': 'AUTO'}}, headers={'Idempotency-Key': str(uuid4())}).status_code == 409
    login(client, 'dispatch', 'acme', 'dispatcher')
    with owner_engine.begin() as db:
        db.execute(text('UPDATE connector_connections SET credentials=NULL WHERE id=:id'), {'id': identity})
    assert save(client, saved, dispatch_mode='AUTO').status_code == 409
    # Native TMS needs no connector; existing AUTO survives preference/profile saves and can always be turned off.
    enabled = save(client, saved, tms_workspace=False, dispatch_mode='AUTO')
    assert enabled.status_code == 200, enabled.text
    switched = save(client, enabled.json(), tms_workspace=True)
    assert switched.status_code == 200, switched.text
    disabled = save(client, switched.json(), dispatch_mode='MANUAL')
    assert disabled.status_code == 200
    assert save(client, disabled.json(), dispatch_mode='AUTO').status_code == 409
