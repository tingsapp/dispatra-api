"""Shipper images persist in their own tenant profile and survive directory edits."""
import base64
import subprocess
import sys
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from sqlalchemy import text
from conftest import HEADERS, login, post, owner_engine
from test_manual_operations import BASE, setup, check

PNG = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nprofile').decode()
JPEG = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xffprofile').decode()


def save(portal, profile, **changes):
    return portal.patch(BASE + '/shipper/profile', json={
        'version': profile['version'], 'phone': profile['phone'], 'warehouse': profile['warehouse'], **changes,
    }, headers={'Idempotency-Key': str(uuid4())})


def test_upload_replace_preserve_remove_and_isolation(setup):
    w = setup
    other = check(post(w['client'], BASE + '/shippers', w['shipper_body'] | {
        'email': 'second@example.com', 'phone': '6045550199'}), 201)
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        profile = check(portal.get(BASE + '/shipper/profile'))
        assert profile['logo_url'] == ''
        saved = check(save(portal, profile, logo_url=PNG))
        assert saved['logo_url'] == PNG
        assert check(portal.get(BASE + '/shipper/profile'))['logo_url'] == PNG
        assert save(portal, profile, logo_url=JPEG).status_code == 409
        # Older clients and dispatcher edits must preserve the shipper's image.
        saved = check(save(portal, saved))
        assert saved['logo_url'] == PNG
        edited = check(w['client'].put(BASE + '/shippers/' + saved['id'], json={
            'version': saved['version'], 'data': w['shipper_body'],
        }, headers={'Idempotency-Key': str(uuid4())}))
        assert edited['logo_url'] == PNG
        assert check(w['client'].get(BASE + '/shippers/' + other['id']))['logo_url'] == ''
        assert portal.get('/api/v1/companies/other/shipper/profile').status_code == 404
        assert save(portal, edited, logo_url=JPEG, shipper_id=other['id']).status_code == 422
        assert save(portal, edited, logo_url=JPEG, rate_card_id=None).status_code == 422
        full_size = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (200 * 1024 - 8)).decode()
        full = check(save(portal, edited, logo_url=full_size))
        assert full['logo_url'] == full_size
        replaced = check(save(portal, full, logo_url=JPEG))
        assert replaced['logo_url'] == JPEG
        body = {'version': replaced['version'], 'phone': replaced['phone'], 'warehouse': replaced['warehouse'], 'logo_url': ''}
        key = str(uuid4())
        response = portal.patch(BASE + '/shipper/profile', json=body, headers={'Idempotency-Key': key})
        assert check(response)['logo_url'] == ''
        assert check(portal.patch(BASE + '/shipper/profile', json=body, headers={'Idempotency-Key': key})) == response.json()
        assert portal.patch(BASE + '/shipper/profile', json=body | {'logo_url': PNG}, headers={'Idempotency-Key': key}).status_code == 409
        assert check(portal.get(BASE + '/shipper/profile'))['logo_url'] == ''


@pytest.mark.parametrize('image', [
    'data:image/svg+xml;base64,PHN2Zy8+', 'javascript:alert(1)',
    'data:image/png;base64,invalid!',
    'data:image/png;base64,' + base64.b64encode(b'not a PNG').decode(),
    'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (200 * 1024)).decode(),
], ids=['svg', 'script', 'base64', 'signature', 'oversize'])
def test_reject_invalid_images(setup, image):
    w = setup
    with TestClient(app, headers=HEADERS) as portal:
        login(portal, 'customer', 'acme', w['shipper']['email'], w['shipper']['initial_password'])
        profile = check(portal.get(BASE + '/shipper/profile'))
        assert save(portal, profile, logo_url=image).status_code == 422
        assert check(portal.get(BASE + '/shipper/profile'))['version'] == profile['version']


def test_profile_body_limits_and_company_logo_maximum(setup):
    w = setup
    profile = check(w['client'].get(BASE + '/settings'))
    full_size = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (200 * 1024 - 8)).decode()
    body = {'version': profile['version'], 'data': profile['data'] | {'logo_url': full_size}}
    result = w['client'].put(BASE + '/settings', json=body, headers={'Idempotency-Key': str(uuid4())})
    assert check(result)['data']['logo_url'] == full_size
    assert w['client'].post(BASE + '/shippers', content='x' * 262145).status_code == 413
    assert w['client'].patch(BASE + '/shipper/profile', content='x' * 524289).status_code == 413


def test_migration_preserves_existing_shipper_details(setup):
    w = setup
    with owner_engine.connect() as db:
        before = db.execute(text('SELECT id, name, email, warehouse, version FROM shippers ORDER BY id')).all()
    subprocess.run([sys.executable, '-m', 'alembic', 'downgrade', '0062_repair_run_purpose'], check=True)
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True)
    with owner_engine.connect() as db:
        after = db.execute(text('SELECT id, name, email, warehouse, version FROM shippers ORDER BY id')).all()
        assert after == before
        assert db.scalar(text('SELECT count(*) FROM shippers WHERE logo_url <> :empty'), {'empty': ''}) == 0
    assert check(w['client'].get(BASE + '/shippers/' + w['shipper']['id']))['logo_url'] == ''
