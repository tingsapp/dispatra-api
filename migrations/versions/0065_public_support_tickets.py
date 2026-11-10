"""Durable public support tickets and their fixed-recipient email queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0065_public_support_tickets'
down_revision = '0064_dispatcher_chat'
branch_labels = depends_on = None


def upgrade():
    op.create_table('support_tickets',
        sa.Column('id', UUID(), primary_key=True),
        sa.Column('session_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('request_id', UUID(), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('number', sa.String(24), nullable=False, unique=True),
        sa.Column('email', sa.String(254), nullable=False),
        sa.Column('subject', sa.String(160), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivery_status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True)),
        sa.Column('claim_id', UUID()),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('error_code', sa.String(80)),
        sa.CheckConstraint("delivery_status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN')"),
        sa.CheckConstraint('attempts >= 0'))
    op.create_index('support_tickets_pending', 'support_tickets', ['available_at'],
        postgresql_where=sa.text("delivery_status IN ('PENDING','SENDING')"))
    op.execute('ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE support_tickets FORCE ROW LEVEL SECURITY')
    own = "session_hash = nullif(current_setting('app.support_session', true), '')"
    platform = "current_setting('app.platform', true) = 'true'"
    op.execute(f'CREATE POLICY support_read ON support_tickets FOR SELECT USING ({own} OR {platform})')
    op.execute(f'CREATE POLICY support_create ON support_tickets FOR INSERT WITH CHECK ({own})')
    op.execute(f'CREATE POLICY support_delivery ON support_tickets FOR UPDATE USING ({platform}) WITH CHECK ({platform})')
    op.execute('GRANT SELECT, INSERT, UPDATE ON support_tickets TO dispatra_app')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM support_tickets)')):
        raise RuntimeError('Support tickets must be archived before downgrading; refusing to delete requests.')
    op.drop_table('support_tickets')
