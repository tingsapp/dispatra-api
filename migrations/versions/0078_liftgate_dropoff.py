"""Split Power Liftgate by stop: every company with a Liftgate charge gets a matching drop-off charge, and the default-named one becomes the pickup charge."""
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import text

revision = '0078_liftgate_dropoff'
down_revision = '0077_stop_report_reasons'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(text("SELECT organization_id, active, data FROM catalog_entries WHERE kind = 'ACCESSORIAL' AND code = 'LIFTGATE'"))
    for org_id, active, data in rows:
        # The drop-off charge starts at the company's own Liftgate rate and terms.
        payload = {**data, 'name': 'Power Liftgate at Drop-off', 'required_equipment': ['LIFTGATE']}
        connection.execute(text('''INSERT INTO catalog_entries
            (id, organization_id, kind, code, active, data, version, created_at, updated_at)
            VALUES (:id, :org, 'ACCESSORIAL', 'LIFTGATE_DROPOFF', :active, CAST(:data AS jsonb), 1, now(), now())
            ON CONFLICT (organization_id, kind, code) DO NOTHING'''),
            {'id': uuid5(NAMESPACE_URL, f'dispatra:{org_id}:pricing-v1:acc_liftgate_dropoff'), 'org': org_id, 'active': active, 'data': json.dumps(payload)})
    # A renamed Liftgate keeps its company's name.
    connection.execute(text('''UPDATE catalog_entries SET data = jsonb_set(data, '{name}', '"Power Liftgate at Pickup"'), version = version + 1, updated_at = now()
        WHERE kind = 'ACCESSORIAL' AND code = 'LIFTGATE' AND data->>'name' = 'Power Liftgate' '''))


def downgrade():
    # Saved orders and frozen pricing can refer to the drop-off catalogue identity.
    pass
