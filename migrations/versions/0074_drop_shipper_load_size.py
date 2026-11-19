"""Shippers no longer carry a default load size; their default Rate Card (the first attached card) prefills new Orders.
Order load sizes are unchanged."""
from alembic import op

revision = '0074_drop_shipper_load_size'
down_revision = '0073_load_size'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE shippers DROP CONSTRAINT shippers_default_load_size')
    op.execute('ALTER TABLE shippers DROP COLUMN default_load_size')


def downgrade():
    op.execute('ALTER TABLE shippers ADD COLUMN default_load_size varchar(20)')
    op.execute("ALTER TABLE shippers ADD CONSTRAINT shippers_default_load_size CHECK (default_load_size IN ('FTL','LTL','SMALL_PARCEL'))")
