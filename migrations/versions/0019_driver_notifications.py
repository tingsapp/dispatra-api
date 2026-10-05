"""Driver-owned assignment inbox; immutable message content and versioned read state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0019_driver_notifications'
down_revision = '0018_shipper_delivery_proof'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('driver_notifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('driver_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('route_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('body', sa.String(500), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True)),
        sa.Column('version', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('organization_id', 'id'),
        sa.ForeignKeyConstraint(['organization_id','driver_id'], ['drivers.organization_id','drivers.id']),
        sa.ForeignKeyConstraint(['organization_id','order_id'], ['orders.organization_id','orders.id']),
        sa.ForeignKeyConstraint(['organization_id','route_id'], ['routes.organization_id','routes.id']))
    op.create_index('ix_driver_notifications_inbox', 'driver_notifications', ['organization_id','driver_id','created_at'])
    op.execute('ALTER TABLE driver_notifications ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE driver_notifications FORCE ROW LEVEL SECURITY')
    scope = """organization_id::text = nullif(current_setting('app.organization_id', true), '')
        AND coalesce(current_setting('app.shipper_id', true), '') = ''
        AND (coalesce(current_setting('app.driver_id', true), '') = '' OR driver_id::text = current_setting('app.driver_id', true))"""
    op.execute(f'CREATE POLICY tenant_isolation ON driver_notifications USING ({scope}) WITH CHECK ({scope})')
    op.execute('GRANT SELECT, INSERT ON driver_notifications TO dispatra_app')
    op.execute('GRANT UPDATE (read_at, version, updated_at) ON driver_notifications TO dispatra_app')


def downgrade():
    op.drop_table('driver_notifications')
