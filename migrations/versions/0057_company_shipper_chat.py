"""Allow private ongoing company/shipper chat; preserve immutable order history."""
from alembic import op
from sqlalchemy import text
revision = '0057_company_shipper_chat'
down_revision = '0056_company_driver_chat'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE conversations DROP CONSTRAINT company_chat_kind')
    op.execute("ALTER TABLE conversations ADD CONSTRAINT company_chat_kind CHECK (order_id IS NOT NULL OR kind IN ('DISPATCH_DRIVER', 'DISPATCH_SHIPPER'))")
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversations_order_kind_driver')
    op.execute('ALTER TABLE conversations ADD CONSTRAINT conversations_order_kind_driver UNIQUE NULLS NOT DISTINCT (organization_id, order_id, kind, driver_id, shipper_id)')
    op.execute("CREATE INDEX ix_conversations_shipper_company ON conversations (organization_id, shipper_id, last_message_at DESC) WHERE kind = 'DISPATCH_SHIPPER'")


def downgrade():
    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM conversations WHERE order_id IS NULL AND kind = 'DISPATCH_SHIPPER')")):
        raise RuntimeError('Company/shipper history exists; refusing a destructive downgrade.')
    op.execute('DROP INDEX ix_conversations_shipper_company')
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversations_order_kind_driver')
    op.execute('ALTER TABLE conversations ADD CONSTRAINT conversations_order_kind_driver UNIQUE NULLS NOT DISTINCT (organization_id, order_id, kind, driver_id)')
    op.execute('ALTER TABLE conversations DROP CONSTRAINT company_chat_kind')
    op.execute("ALTER TABLE conversations ADD CONSTRAINT company_chat_kind CHECK (order_id IS NOT NULL OR kind = 'DISPATCH_DRIVER')")
