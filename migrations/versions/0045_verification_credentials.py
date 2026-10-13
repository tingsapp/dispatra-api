"""Invalidate outstanding verification on password rotation."""
from alembic import op
revision = '0045_verification_credentials'
down_revision = '0044_email_verification'
branch_labels = None
depends_on = None
def upgrade():
    op.execute("ALTER TABLE email_verifications ADD COLUMN credential_hash text NOT NULL DEFAULT ''")
    # No credential proof exists for older pending challenges; a fresh login reissues them.
def downgrade():
    op.execute('ALTER TABLE email_verifications DROP COLUMN credential_hash')
