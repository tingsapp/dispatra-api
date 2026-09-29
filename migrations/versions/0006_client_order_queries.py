"""Index canonical Order fields used by the existing Orders screen.

The booking JSON snapshot remains immutable history; service_id is a query key.
"""
from alembic import op

revision = '0006_client_order_queries'
down_revision = '0005_stop_arrival'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE orders ADD COLUMN service_id uuid')
    op.execute("UPDATE orders SET service_id = (facts ->> 'service_id')::uuid")
    op.execute('ALTER TABLE orders ALTER COLUMN service_id SET NOT NULL')
    op.execute('ALTER TABLE orders ADD CONSTRAINT orders_service_tenant FOREIGN KEY (organization_id, service_id) REFERENCES catalog_entries (organization_id, id)')
    op.execute('CREATE INDEX ix_orders_company_schedule ON orders (organization_id, scheduled_at, id)')
    op.execute('CREATE INDEX ix_orders_company_status_schedule ON orders (organization_id, status, scheduled_at)')
    op.execute('CREATE INDEX ix_orders_company_service_schedule ON orders (organization_id, service_id, scheduled_at)')


def downgrade():
    op.execute('DROP INDEX ix_orders_company_service_schedule')
    op.execute('DROP INDEX ix_orders_company_status_schedule')
    op.execute('DROP INDEX ix_orders_company_schedule')
    op.execute('ALTER TABLE orders DROP CONSTRAINT orders_service_tenant')
    op.execute('ALTER TABLE orders DROP COLUMN service_id')
