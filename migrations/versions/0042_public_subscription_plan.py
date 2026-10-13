"""Publish the single plan's terms while keeping all configuration writes owner-only."""
from alembic import op

revision = '0042_public_subscription_plan'
down_revision = '0041_subscription_plan'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE POLICY public_plan_read ON subscription_plans FOR SELECT USING (id = '8eb730cb-226b-4ec1-a3fc-132279938d62')")


def downgrade():
    op.execute('DROP POLICY public_plan_read ON subscription_plans')
