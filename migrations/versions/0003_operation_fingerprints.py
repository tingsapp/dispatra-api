"""Use a slow verifier for idempotency payloads containing credentials."""
from alembic import op
revision = '0003_operation_fingerprints'
down_revision = '0002_account_outbox'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('ALTER TABLE operations ALTER COLUMN payload_hash TYPE text')

def downgrade():
    # Preserve strong credential verifiers; do not truncate them to a fast hash.
    pass
