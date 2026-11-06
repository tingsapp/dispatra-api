"""Long code tests must not suppress scheduled Technical health checks."""
from alembic import op
import sqlalchemy as sa

revision = '0062_repair_run_purpose'
down_revision = '0061_local_code_repair'
branch_labels = depends_on = None


def upgrade():
    op.add_column('agent_runs', sa.Column('purpose', sa.String(16), nullable=False, server_default='GENERAL'))


def downgrade():
    op.drop_column('agent_runs', 'purpose')
