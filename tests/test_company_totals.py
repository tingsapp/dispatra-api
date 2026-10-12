"""Owner company totals count scoped records, including archived/history, through restricted PostgreSQL."""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Operation, now
from app.operations.models import Catalog, Driver, Order, Shipper
from conftest import company, login, owner_engine, post

ROOT = '/api/v1/platform/organizations'
FIELDS = ('shipper_count', 'driver_count', 'order_count')
ADDED_FIELDS = (*FIELDS, 'phone', 'address', 'settings_version', 'logo_url')


def totals(response):
    assert response.status_code == 200, response.text
    data = response.json()
    return tuple(data[field] for field in FIELDS)


@pytest.fixture
def records(client):
    login(client)
    organizations = [company(client, slug) for slug in ('acme', 'other')]
    with Session(owner_engine) as db, db.begin():
        for organization, size in zip(organizations, (3, 1)):
            org_id = UUID(organization['id'])
            service = db.scalar(select(Catalog.id).where(Catalog.organization_id == org_id, Catalog.kind == 'SERVICE'))
            shippers = [Shipper(organization_id=org_id, number=f'S-{i}', name=f'Shipper {i}',
                status='INACTIVE' if i else 'ACTIVE', archived_at=now() if i else None) for i in range(size)]
            db.add_all(shippers); db.flush()
            db.add_all([Driver(organization_id=org_id, number=f'D-{i}', name=f'Driver {i}', email=f'driver{i}@example.com',
                phone='', address={}, service_city='', data={}, active=False, archived_at=now()) for i in range(size)])
            db.add_all([Order(organization_id=org_id, number=f'O-{i}', shipper_id=shippers[0].id,
                billing_shipper_id=shippers[0].id, service_id=service, source='MANUAL',
                status='CANCELLED' if i else 'COMPLETED', scheduled_at=now(), facts={}, booking={}, pricing={}) for i in range(size)])
    return organizations


@pytest.mark.parametrize('index,expected', [(0, (3, 3, 3)), (1, (1, 1, 1))])
def test_totals_are_company_scoped_and_count_records_instead_of_login_accounts(client, records, index, expected):
    org = records[index]
    response = client.get(f"{ROOT}/{org['id']}")
    assert totals(response) == expected
    assert response.json()['shipper_account_count'] == response.json()['driver_account_count'] == 0
    login(client, 'dispatch', org['slug'], 'dispatcher')
    assert client.get(f"{ROOT}/{org['id']}").status_code == 403


def test_empty_company_totals_are_zero(client):
    login(client)
    org = company(client)
    assert totals(client.get(f"{ROOT}/{org['id']}")) == (0, 0, 0)


@pytest.mark.parametrize('action', ['rename', 'suspend', 'overview'])
def test_company_command_replays_from_before_totals_remain_readable(client, records, action):
    org = records[0]
    key = str(uuid4())
    if action == 'suspend':
        invoke = lambda: post(client, f"{ROOT}/{org['id']}/suspend", {'version': 1}, key)
    else:
        body = {'version': 1, 'name': 'Saved company'}
        path = f"{ROOT}/{org['id']}"
        if action == 'overview': body.update(active=True); path += '/overview'
        invoke = lambda: client.patch(path, json=body, headers={'Idempotency-Key': key})
    original = invoke()
    assert original.status_code == 200, original.text
    with Session(owner_engine) as db, db.begin():
        operation = db.scalar(select(Operation).where(Operation.key.endswith(key)))
        legacy = dict(operation.result)
        if action == 'overview': legacy['company'] = {k: v for k, v in legacy['company'].items() if k not in ADDED_FIELDS}
        else: legacy = {k: v for k, v in legacy.items() if k not in ADDED_FIELDS}
        operation.result = legacy
    replay = invoke()
    assert replay.status_code == 200, replay.text
    data = replay.json()['company'] if action == 'overview' else replay.json()
    assert tuple(data[field] for field in FIELDS) == (3, 3, 3)
    assert data['phone'] == data['address'] == '' and data['settings_version'] >= 1
    assert data['logo_url'] == ''
    assert data['version'] == 2, 'replay must not apply writes again'
    assert client.get(f"{ROOT}/{org['id']}").json()['version'] == 2
