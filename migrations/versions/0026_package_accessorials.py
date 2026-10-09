"""Give every company configurable Fragile and DG package charges without replacing edited rates."""
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = '0026_package_accessorials'
down_revision = '0025_dispatch_agent'
branch_labels = None
depends_on = None

DEFAULTS = (
    ('acc_fragile', 'FRAGILE', 'Fragile', 'Extra handling for fragile packages.', '15'),
    ('acc_dg', 'DG', 'DG', 'Dangerous goods handling for each flagged package.', '25'),
)


def upgrade():
    connection = op.get_bind()
    for (org_id,) in connection.execute(text('SELECT id FROM organizations')):
        for source, code, name, description, amount in DEFAULTS:
            identity = uuid5(NAMESPACE_URL, f'dispatra:{org_id}:pricing-v1:{source}')
            payload = {'name': name, 'description': description, 'amount': amount,
                       'taxable': True, 'fuel_eligible': False, 'exclusive_vehicle': False,
                       'pallet_capacity': 0, 'equipment': [], 'required_equipment': [], 'required_crew': 1}
            connection.execute(text('''INSERT INTO catalog_entries
                (id, organization_id, kind, code, active, data, version, created_at, updated_at)
                VALUES (:id, :org, 'ACCESSORIAL', :code, true, CAST(:data AS jsonb), 1, now(), now())
                ON CONFLICT (organization_id, kind, code) DO NOTHING'''),
                {'id': identity, 'org': org_id, 'code': code, 'data': json.dumps(payload)})
    connection.execute(text('''UPDATE catalog_entries SET data = data || jsonb_build_object(
        'name', 'Fragile', 'description', 'Extra handling for fragile packages.'),
        version = version + 1, updated_at = now()
        WHERE kind = 'ACCESSORIAL' AND code = 'FRAGILE'
          AND data->>'name' = 'Fragile Blanket Wrap'
          AND data->>'description' = 'Padded furniture blankets, protective corner guards, and tie-down strap security.' '''))


def downgrade():
    # Saved orders and frozen pricing can refer to these catalogue identities.
    pass
