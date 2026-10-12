"""Company list logos use shared Profile settings without changing scope or pagination."""
from sqlalchemy import text
from conftest import company, login, owner_engine

ROOT = '/api/v1/platform/organizations'
LOGO = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='


def test_company_list_logo_matches_profile_and_preserves_pagination_and_missing_settings(client):
    login(client)
    acme, other, legacy = [company(client, slug) for slug in ('acme', 'other', 'legacy')]
    assert all(row['logo_url'] == '' for row in client.get(ROOT).json())
    login(client, 'dispatch', 'acme', 'dispatcher')
    path = '/api/v1/companies/acme/settings'
    profile = client.get(path).json()
    profile['data'].update(logo_url=LOGO, phone='6045550100')
    saved = client.put(path, json={'version': profile['version'], 'data': profile['data']}, headers={'Idempotency-Key': 'company-logo-save'})
    assert saved.status_code == 200, saved.text
    assert client.get(ROOT).status_code == 403
    login(client)
    with owner_engine.begin() as db:
        db.execute(text('DELETE FROM company_settings WHERE organization_id = :org'), {'org': legacy['id']})
    rows = client.get(ROOT).json()
    assert [row['slug'] for row in rows] == ['acme', 'legacy', 'other']
    assert [row['logo_url'] for row in rows] == [LOGO, '', '']
    assert all('phone' not in row and 'data' not in row for row in rows), 'list returns only the logo from settings'
    assert client.get(f"{ROOT}/{acme['id']}").json()['logo_url'] == LOGO
    assert client.get(f"{ROOT}/{other['id']}").json()['logo_url'] == ''
    assert client.get(ROOT, params={'search': 'acm'}).json()[0]['logo_url'] == LOGO
    first = client.get(ROOT, params={'limit': 1}).json()
    second = client.get(ROOT, params={'limit': 1, 'after': first[0]['id']}).json()
    assert first[0]['logo_url'] == LOGO and second[0]['slug'] == 'legacy' and second[0]['logo_url'] == ''
    login(client, 'dispatch', 'acme', 'dispatcher')
    profile = client.get(path).json()
    profile['data']['logo_url'] = ''
    assert client.put(path, json={'version': profile['version'], 'data': profile['data']}, headers={'Idempotency-Key': 'company-logo-remove'}).status_code == 200
    login(client)
    assert client.get(ROOT, params={'search': 'acme'}).json()[0]['logo_url'] == ''
