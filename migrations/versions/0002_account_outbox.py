"""Transactional account events; delivery workers are a later milestone."""
from alembic import op
revision = '0002_account_outbox'
down_revision = '0001_accounts'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""CREATE TABLE outbox_events (
        id uuid PRIMARY KEY,
        organization_id uuid REFERENCES organizations(id),
        event_type varchar(80) NOT NULL,
        entity_id uuid NOT NULL,
        created_at timestamptz NOT NULL,
        published_at timestamptz
    )""")
    op.execute('CREATE INDEX ix_outbox_events_organization_id ON outbox_events(organization_id)')
    op.execute('GRANT SELECT, INSERT ON outbox_events TO dispatra_app')
    op.execute('ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY tenant_isolation ON outbox_events USING (
        organization_id::text = nullif(current_setting('app.organization_id', true), '')
        OR current_setting('app.platform', true) = 'true'
    )""")

def downgrade():
    op.drop_table('outbox_events')
