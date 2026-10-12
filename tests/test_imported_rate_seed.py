"""Upgrade existing companies without overwriting or duplicating their imported cards."""
import importlib.util
from pathlib import Path

from sqlalchemy import text

from conftest import company, login, owner_engine, post


def test_imported_card_backfill_is_additive_and_repeatable(client, monkeypatch):
    login(client)
    company(client)
    company(client, 'custom')
    company(client, 'conflict')
    path = Path(__file__).parents[1] / 'migrations/versions/0035_imported_rate_card.py'
    spec = importlib.util.spec_from_file_location('imported_rate_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with owner_engine.begin() as connection:
        # Model companies created before this preset, a custom archived card,
        # and a company that already uses the reserved preset code.
        connection.execute(text("""DELETE FROM rate_cards WHERE code = 'IMPORTED'
            AND organization_id IN (SELECT id FROM organizations WHERE slug IN ('acme', 'conflict'))"""))
        connection.execute(text("""UPDATE rate_cards SET code = 'CUSTOM-TMS', active = false,
            data = data || '{"name":"Custom imported terms"}'::jsonb
            WHERE code = 'IMPORTED' AND organization_id = (SELECT id FROM organizations WHERE slug = 'custom')"""))
        connection.execute(text("""INSERT INTO rate_cards
            (id, organization_id, code, active, is_default, data, version, created_at, updated_at)
            SELECT gen_random_uuid(), organization_id, 'IMPORTED', true, false, data, 1, now(), now()
            FROM rate_cards WHERE code = 'NBH-DIRECT'
              AND organization_id = (SELECT id FROM organizations WHERE slug = 'conflict')"""))
        before = connection.execute(text('SELECT id, code, active, is_default, data, version, updated_at FROM rate_cards ORDER BY id')).all()
        monkeypatch.setattr(migration.op, 'get_bind', lambda: connection)
        migration.upgrade()
        migration.upgrade()
        after = connection.execute(text('SELECT id, code, active, is_default, data, version, updated_at FROM rate_cards ORDER BY id')).all()
        assert all(row in after for row in before)
        assert len(after) == len(before) + 1
        added, = [row for row in after if row not in before]
        assert added.code == 'IMPORTED' and added.active and not added.is_default
        assert added.data['method'] == 'IMPORTED' and added.data['name'] == 'Imported price'
    # The normal dispatcher seed uses the same identity and skips custom cards.
    for slug in ('acme', 'custom', 'conflict'):
        login(client, 'dispatch', slug, 'dispatcher')
        result = post(client, f'/api/v1/companies/{slug}/pricing/seed', {})
        assert result.status_code == 200 and result.json()['created'] == 0
