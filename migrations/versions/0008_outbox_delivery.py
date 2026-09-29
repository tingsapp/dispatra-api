"""Lease and retry metadata for tenant-scoped transactional outbox workers."""
from alembic import op

revision = '0008_outbox_delivery'
down_revision = '0007_monitor_indexes'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE outbox_events ADD COLUMN available_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP')
    op.execute('ALTER TABLE outbox_events ADD COLUMN attempts integer NOT NULL DEFAULT 0')
    op.execute('ALTER TABLE outbox_events ADD COLUMN claimed_at timestamptz')
    op.execute('ALTER TABLE outbox_events ADD COLUMN claimed_by varchar(80)')
    op.execute('ALTER TABLE outbox_events ADD COLUMN last_error_code varchar(80)')
    op.execute('CREATE INDEX ix_outbox_pending ON outbox_events (organization_id, available_at, created_at, id) WHERE published_at IS NULL')
    op.execute('GRANT UPDATE (available_at, attempts, claimed_at, claimed_by, last_error_code, published_at) ON outbox_events TO dispatra_app')


def downgrade():
    op.execute('REVOKE UPDATE ON outbox_events FROM dispatra_app')
    op.execute('DROP INDEX ix_outbox_pending')
    op.execute('ALTER TABLE outbox_events DROP COLUMN last_error_code')
    op.execute('ALTER TABLE outbox_events DROP COLUMN claimed_by')
    op.execute('ALTER TABLE outbox_events DROP COLUMN claimed_at')
    op.execute('ALTER TABLE outbox_events DROP COLUMN attempts')
    op.execute('ALTER TABLE outbox_events DROP COLUMN available_at')
