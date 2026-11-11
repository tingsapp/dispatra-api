"""Canonicalize stored commercial and dispatch zone postcodes to FSAs."""
from alembic import op

revision = '0067_zone_fsa_prefixes'
down_revision = '0066_email_draft_completion'
branch_labels = depends_on = None


def upgrade():
    # Zone configuration is an area rule: every saved full Canadian postal code
    # becomes its uppercased three-character Forward Sortation Area (FSA).
    op.execute("""
        UPDATE rate_cards
        SET data = jsonb_set(data, '{zones}', (
            SELECT coalesce(jsonb_agg(jsonb_set(zone, '{postal_codes}', (
                SELECT coalesce(jsonb_agg(DISTINCT to_jsonb(upper(left(replace(code, ' ', ''), 3)))), '[]'::jsonb)
                FROM jsonb_array_elements_text(coalesce(zone->'postal_codes', '[]'::jsonb)) AS item(code)
            ), true)), '[]'::jsonb)
            FROM jsonb_array_elements(coalesce(data->'zones', '[]'::jsonb)) AS zone
        ), true)
        WHERE data ? 'zones'
    """)
    op.execute("""
        UPDATE company_settings
        SET data = jsonb_set(data, '{dispatch_zones}', (
            SELECT coalesce(jsonb_agg(jsonb_set(zone, '{postal_codes}', (
                SELECT coalesce(jsonb_agg(DISTINCT to_jsonb(upper(left(replace(code, ' ', ''), 3)))), '[]'::jsonb)
                FROM jsonb_array_elements_text(coalesce(zone->'postal_codes', '[]'::jsonb)) AS item(code)
            ), true)), '[]'::jsonb)
            FROM jsonb_array_elements(coalesce(data->'dispatch_zones', '[]'::jsonb)) AS zone
        ), true)
        WHERE data ? 'dispatch_zones'
    """)


def downgrade():
    raise RuntimeError('FSA migration cannot restore the discarded final three postal-code characters.')
