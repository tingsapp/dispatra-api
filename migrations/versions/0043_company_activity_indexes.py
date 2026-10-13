"""Index the company-scoped chronological activity projection without changing row access."""
from alembic import op

revision = '0043_company_activity_indexes'
down_revision = '0042_public_subscription_plan'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_agent_logs_company_recent', 'agent_logs', ['organization_id', 'created_at', 'id'])
    op.create_index('ix_events_company_recent', 'events', ['organization_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_events_company_recent', table_name='events')
    op.drop_index('ix_agent_logs_company_recent', table_name='agent_logs')
