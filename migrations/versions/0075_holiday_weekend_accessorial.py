"""Give every company the automatic Holiday / Weekend Delivery charge without replacing an existing entry."""
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = '0075_holiday_weekend_accessorial'
down_revision = '0074_drop_shipper_load_size'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    payload = {'name': 'Holiday / Weekend Delivery',
               'description': 'Added automatically when a delivery falls on a Saturday, Sunday or company holiday.',
               'amount': '25', 'taxable': True, 'fuel_eligible': False, 'exclusive_vehicle': False,
               'pallet_capacity': 0, 'equipment': [], 'required_equipment': [], 'required_crew': 1}
    for (org_id,) in connection.execute(text('SELECT id FROM organizations')):
        connection.execute(text('''INSERT INTO catalog_entries
            (id, organization_id, kind, code, active, data, version, created_at, updated_at)
            VALUES (:id, :org, 'ACCESSORIAL', 'HOLIDAY_WEEKEND', true, CAST(:data AS jsonb), 1, now(), now())
            ON CONFLICT (organization_id, kind, code) DO NOTHING'''),
            {'id': uuid5(NAMESPACE_URL, f'dispatra:{org_id}:pricing-v1:acc_holiday_weekend'), 'org': org_id, 'data': json.dumps(payload)})


def downgrade():
    # Saved orders and frozen pricing can refer to this catalogue identity.
    pass
