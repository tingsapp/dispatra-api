"""Order load size: FTL, LTL or Small parcel (`orders.facts.load_size`).

A full truckload has no service level, so `orders.service_id` becomes nullable. Shippers may carry a default load size
for new Orders. Existing Orders and Shippers keep their data; their load size stays unset."""
from alembic import op

revision = '0073_load_size'
down_revision = '0072_shipper_rate_cards'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE orders ALTER COLUMN service_id DROP NOT NULL')
    op.execute('ALTER TABLE shippers ADD COLUMN default_load_size varchar(20)')
    op.execute("ALTER TABLE shippers ADD CONSTRAINT shippers_default_load_size CHECK (default_load_size IN ('FTL','LTL','SMALL_PARCEL'))")


def downgrade():
    op.execute('ALTER TABLE shippers DROP CONSTRAINT shippers_default_load_size')
    op.execute('ALTER TABLE shippers DROP COLUMN default_load_size')
    op.execute('ALTER TABLE orders ALTER COLUMN service_id SET NOT NULL')
