"""Add the missing Imported price preset without replacing existing company choices."""
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = '0035_imported_rate_card'
down_revision = '0034_driver_push'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    payload = json.dumps({
        'name': 'Imported price', 'method': 'IMPORTED',
        'base_fee': '0', 'included_km': '0', 'per_km': '0', 'fixed_amount': '0',
        'hourly_rate': '0', 'minimum_minutes': 0, 'increment_minutes': 30,
        'minimum_subtotal': '0', 'dimensional_divisor': '5000',
        'zones': [], 'weight_bands': [], 'apply_fuel': False, 'apply_service': False,
        'apply_vehicle': False, 'apply_accessorials': False,
    })
    # Do not recreate an imported card a company intentionally archived, or add a
    # second imported card when it already uses a custom name/code.
    for (org_id,) in connection.execute(text("""SELECT id FROM organizations o
        WHERE NOT EXISTS (SELECT 1 FROM rate_cards r WHERE r.organization_id = o.id
            AND (r.data->>'method' = 'IMPORTED' OR r.code = 'IMPORTED'))""")):
        identity = uuid5(NAMESPACE_URL, f'dispatra:{org_id}:pricing-v1:rc_imported')
        connection.execute(text("""INSERT INTO rate_cards
            (id, organization_id, code, active, is_default, data, version, created_at, updated_at)
            VALUES (:id, :org, 'IMPORTED', true, false, CAST(:data AS jsonb), 1, now(), now())
            ON CONFLICT (organization_id, code) DO NOTHING"""),
            {'id': identity, 'org': org_id, 'data': payload})


def downgrade():
    # Orders and frozen pricing may reference these stable identities.
    pass
