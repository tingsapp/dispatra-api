"""Let a shipper read the route visits of its own Order stops, so delivery proof is visible.

Read-only and additive: the tenant_isolation policy still hides Routes and every other visit from
shippers and still governs all writes. Order stops are already limited to the shipper's own Orders.
"""
from alembic import op

revision = '0018_shipper_delivery_proof'
down_revision = '0017_drop_driver_payout'
branch_labels = None
depends_on = None


def upgrade():
    tenant = "organization_id::text = nullif(current_setting('app.organization_id', true), '')"
    shipper = "coalesce(current_setting('app.shipper_id', true), '') <> ''"
    op.execute(f'CREATE POLICY shipper_delivery_proof ON route_stops FOR SELECT USING (({tenant}) AND {shipper} AND stop_id IN (SELECT id FROM order_stops))')


def downgrade():
    op.execute('DROP POLICY shipper_delivery_proof ON route_stops')
