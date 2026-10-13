"""Add 30-day trial dates without changing established companies or account identities."""
from alembic import op
revision = '0039_trial_signup'
down_revision = '0038_role_login'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE organizations ADD COLUMN trial_started_at timestamptz, ADD COLUMN trial_ends_at timestamptz, ADD COLUMN trial_activated_at timestamptz')
    op.execute("ALTER TABLE organizations ADD CONSTRAINT trial_dates CHECK ((trial_started_at IS NULL AND trial_ends_at IS NULL AND trial_activated_at IS NULL) OR (trial_started_at IS NOT NULL AND trial_ends_at IS NOT NULL AND trial_ends_at > trial_started_at AND (trial_activated_at IS NULL OR trial_activated_at >= trial_started_at)))")


def downgrade():
    op.execute('ALTER TABLE organizations DROP CONSTRAINT trial_dates, DROP COLUMN trial_started_at, DROP COLUMN trial_ends_at, DROP COLUMN trial_activated_at')
