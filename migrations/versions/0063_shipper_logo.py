"""Persist each shipper's self-service profile image without changing existing details."""
from alembic import op
import sqlalchemy as sa

revision = '0063_shipper_logo'
down_revision = '0062_repair_run_purpose'
branch_labels = depends_on = None


def upgrade():
    op.add_column('shippers', sa.Column('logo_url', sa.Text(), nullable=False, server_default=''))


def downgrade():
    op.drop_column('shippers', 'logo_url')
