"""Allow one company/driver conversation without an Order; retain all existing history."""
from alembic import op
from sqlalchemy import text
revision = '0056_company_driver_chat'
down_revision = '0055_connector_management'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE conversations ALTER COLUMN order_id DROP NOT NULL')
    op.execute("ALTER TABLE conversations ADD CONSTRAINT company_chat_kind CHECK (order_id IS NOT NULL OR kind = 'DISPATCH_DRIVER')")


def downgrade():
    if op.get_bind().scalar(text('SELECT EXISTS (SELECT 1 FROM conversations WHERE order_id IS NULL)')):
        raise RuntimeError('Company chat history exists; refusing a destructive downgrade.')
    op.execute('ALTER TABLE conversations ALTER COLUMN order_id SET NOT NULL')
    op.execute('ALTER TABLE conversations DROP CONSTRAINT company_chat_kind')
