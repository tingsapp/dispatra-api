"""Explicit visit arrival time for driver execution, SLA and hourly settlement."""
from alembic import op
revision = '0005_stop_arrival'
down_revision = '0004_manual_operations'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE route_stops ADD COLUMN arrived_at timestamptz')


def downgrade():
    op.execute('ALTER TABLE route_stops DROP COLUMN arrived_at')
