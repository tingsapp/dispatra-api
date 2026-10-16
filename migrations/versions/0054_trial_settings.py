"""Configurable trial availability and duration; preserve accepted and recorded trials."""
from alembic import op
import sqlalchemy as sa

revision = '0054_trial_settings'
down_revision = '0053_plan_options'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('subscription_plans', sa.Column('trial_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('subscription_plans', sa.Column('trial_days', sa.Integer(), nullable=False, server_default='30'))
    op.create_check_constraint('subscription_plans_trial_days_check', 'subscription_plans', 'trial_days BETWEEN 1 AND 365')
    op.add_column('organizations', sa.Column('trial_duration_days', sa.Integer(), nullable=True))
    # Pending signups already accepted the previous 30-day offer. Settings edits
    # must not revoke it or change their duration before email verification.
    op.execute('UPDATE organizations SET trial_duration_days = 30 WHERE verification_pending = true')
    op.create_check_constraint('organizations_trial_duration_days_check', 'organizations', 'trial_duration_days BETWEEN 1 AND 365')


def downgrade():
    op.drop_constraint('organizations_trial_duration_days_check', 'organizations', type_='check')
    op.drop_column('organizations', 'trial_duration_days')
    op.drop_constraint('subscription_plans_trial_days_check', 'subscription_plans', type_='check')
    op.drop_column('subscription_plans', 'trial_days')
    op.drop_column('subscription_plans', 'trial_enabled')
