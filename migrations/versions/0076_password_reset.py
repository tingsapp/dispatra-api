"""Add password-reset purpose to email verification requests."""
from alembic import op
import sqlalchemy as sa

revision = '0076_password_reset'
down_revision = '0075_holiday_weekend_accessorial'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('email_verifications',
        sa.Column('purpose', sa.String(length=24), nullable=False,
                  server_default='VERIFICATION'))
    op.create_check_constraint(
        'email_verification_purpose',
        'email_verifications',
        "purpose IN ('VERIFICATION','PASSWORD_RESET')",
    )
    op.alter_column('email_verifications', 'purpose', server_default=None)


def downgrade():
    op.drop_constraint('email_verification_purpose', 'email_verifications', type_='check')
    op.drop_column('email_verifications', 'purpose')
