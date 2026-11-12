"""Give every company the default Envelope, Parcel, Pallet and Oversized Package Types without replacing edited ones."""
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = '0068_package_types'
down_revision = '0067_zone_fsa_prefixes'
branch_labels = None
depends_on = None

# (seed id, code, name, description, default weight kg, length, width, height cm) — mirrors seeds/demo_pricing_v1.json.
DEFAULTS = (
    ('pkg_envelope', 'ENVELOPE', 'Envelope', 'Letters and documents.', '0.5', '32', '24', '2'),
    ('pkg_parcel', 'PARCEL', 'Parcel', 'Boxes, bags and other regular packages.', None, None, None, None),
    ('pkg_pallet', 'PALLET', 'Pallet', 'Goods stacked on a standard 48 x 40 in pallet.', None, '121.92', '101.6', '121.92'),
    ('pkg_oversized', 'OVERSIZED', 'Oversized', 'Long, bulky or irregular items such as furniture.', None, None, None, None),
)


def upgrade():
    connection = op.get_bind()
    for (org_id,) in connection.execute(text('SELECT id FROM organizations')):
        for source, code, name, description, weight, length, width, height in DEFAULTS:
            identity = uuid5(NAMESPACE_URL, f'dispatra:{org_id}:pricing-v1:{source}')
            payload = {'name': name, 'description': description, 'amount': '0',
                       'taxable': True, 'fuel_eligible': False, 'exclusive_vehicle': False,
                       'payload_kg': None, 'volume_m3': None, 'length_cm': length, 'width_cm': width, 'height_cm': height,
                       'default_weight_kg': weight, 'pallet_capacity': 0, 'equipment': [], 'required_equipment': [], 'required_crew': 1}
            connection.execute(text('''INSERT INTO catalog_entries
                (id, organization_id, kind, code, active, data, version, created_at, updated_at)
                VALUES (:id, :org, 'PACKAGE_TYPE', :code, true, CAST(:data AS jsonb), 1, now(), now())
                ON CONFLICT (organization_id, kind, code) DO NOTHING'''),
                {'id': identity, 'org': org_id, 'code': code, 'data': json.dumps(payload)})


def downgrade():
    # Saved order items can refer to these catalogue identities.
    pass
