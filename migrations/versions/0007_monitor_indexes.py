"""Index the current Monitor's live-location and open-issue reads."""
from alembic import op

revision = '0007_monitor_indexes'
down_revision = '0006_client_order_queries'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE INDEX ix_driver_locations_company_latest ON driver_locations (organization_id, driver_id, captured_at DESC, id DESC)')
    op.execute('CREATE INDEX ix_operation_issues_company_open ON operation_issues (organization_id, order_id) WHERE resolved = false')


def downgrade():
    op.execute('DROP INDEX ix_operation_issues_company_open')
    op.execute('DROP INDEX ix_driver_locations_company_latest')
