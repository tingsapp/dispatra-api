"""Separate Smart Dispatching default pricing; preserve existing company agreements."""
from alembic import op
import sqlalchemy as sa

revision = '0053_plan_options'
down_revision = '0052_recurring_company_billing'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('subscription_plans', sa.Column('dispatch_monthly_price', sa.Numeric(12, 2), nullable=True))
    op.add_column('subscription_plans', sa.Column('dispatch_currency', sa.String(3), nullable=False, server_default='CAD'))
    op.execute('UPDATE subscription_plans SET dispatch_currency = currency')
    op.create_check_constraint('subscription_plans_dispatch_price_check', 'subscription_plans', 'dispatch_monthly_price >= 0')
    op.create_check_constraint('subscription_plans_dispatch_currency_check', 'subscription_plans', "dispatch_currency ~ '^[A-Z]{3}$'")


def downgrade():
    op.drop_constraint('subscription_plans_dispatch_currency_check', 'subscription_plans', type_='check')
    op.drop_constraint('subscription_plans_dispatch_price_check', 'subscription_plans', type_='check')
    op.drop_column('subscription_plans', 'dispatch_currency')
    op.drop_column('subscription_plans', 'dispatch_monthly_price')
