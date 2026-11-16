"""Archived Orders: a closed Order leaves the Orders lists and chat while its history, billing and analytics stay."""
from alembic import op

revision = '0071_order_archive'
down_revision = '0070_chat_alert_control'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE orders ADD COLUMN archived_at timestamptz')
    op.execute("ALTER TABLE orders ADD CONSTRAINT orders_archived_closed CHECK (archived_at IS NULL OR status IN ('COMPLETED','CANCELLED'))")
    op.execute('CREATE INDEX ix_orders_company_visible ON orders (organization_id, id) WHERE archived_at IS NULL')


def downgrade():
    op.execute('DROP INDEX ix_orders_company_visible')
    op.execute('ALTER TABLE orders DROP CONSTRAINT orders_archived_closed')
    op.execute('ALTER TABLE orders DROP COLUMN archived_at')
