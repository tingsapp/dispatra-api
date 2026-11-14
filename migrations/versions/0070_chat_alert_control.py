"""Dispatcher chat clean-up: hidden Needs Attention warnings and deletable notifications.

`attention_dismissals` hides one computed warning or Operational incident of one Order while that Order keeps the
version it had when hidden (an incident also returns when it becomes more severe). Dispatcher scope only.
Notifications may now be deleted by their recipient; e-mailed ones stay because their delivery record refers to them.
"""
from alembic import op

revision = '0070_chat_alert_control'
down_revision = '0069_default_package_type'
branch_labels = None
depends_on = None

DISPATCHER = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    op.execute("""CREATE TABLE attention_dismissals (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        order_id uuid NOT NULL,
        key varchar(80) NOT NULL,
        order_version integer NOT NULL,
        severity varchar(12),
        dismissed_by uuid NOT NULL REFERENCES users(id),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT attention_dismissals_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT attention_dismissal_once UNIQUE (organization_id, order_id, key),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_attention_dismissals_organization_id ON attention_dismissals (organization_id)')
    op.execute('ALTER TABLE attention_dismissals ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE attention_dismissals FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON attention_dismissals USING ({DISPATCHER}) WITH CHECK ({DISPATCHER})')
    op.execute('GRANT SELECT, INSERT ON attention_dismissals TO dispatra_app')
    op.execute('GRANT UPDATE (order_version, severity, dismissed_by, version, updated_at) ON attention_dismissals TO dispatra_app')
    op.execute('GRANT DELETE ON notifications TO dispatra_app')


def downgrade():
    op.execute('REVOKE DELETE ON notifications FROM dispatra_app')
    op.execute('DROP TABLE attention_dismissals')
