"""Standing company-scoped driver assignment for new Shipper Orders."""
from alembic import op
revision = '0051_shipper_assigned_driver'
down_revision = '0050_company_payment_methods'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE shippers ADD COLUMN assigned_driver_id uuid')
    op.execute('ALTER TABLE shippers ADD CONSTRAINT shippers_assigned_driver_company '
        'FOREIGN KEY (organization_id, assigned_driver_id) REFERENCES drivers(organization_id, id)')


def downgrade():
    # Refuse lossy rollback; historical Order snapshots are retained independently.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM shippers WHERE assigned_driver_id IS NOT NULL) "
        "THEN RAISE EXCEPTION 'Clear saved Shipper driver assignments before downgrade'; END IF; END $$")
    op.execute('ALTER TABLE shippers DROP CONSTRAINT shippers_assigned_driver_company')
    op.execute('ALTER TABLE shippers DROP COLUMN assigned_driver_id')
