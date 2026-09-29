"""Separate directory deletion from editable inactive status."""
from alembic import op

revision = '0010_directory_archival'
down_revision = '0009_driver_numbers'
branch_labels = None
depends_on = None

TABLES = ('shippers', 'drivers', 'vehicles')


def upgrade():
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} ADD COLUMN archived_at timestamptz')
        op.execute(f'CREATE INDEX ix_{table}_company_visible ON {table} (organization_id, id) WHERE archived_at IS NULL')


def downgrade():
    for table in reversed(TABLES):
        op.execute(f'DROP INDEX ix_{table}_company_visible')
        op.execute(f'ALTER TABLE {table} DROP COLUMN archived_at')
