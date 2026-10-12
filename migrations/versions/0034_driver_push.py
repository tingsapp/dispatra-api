"""Driver push: FCM device registrations bound to the native login session, and queued push messages.

A device row is deleted with its session (logout, password change, revocation), so a signed-out phone stops
receiving pushes. `push_messages` are delivered by `app.push.worker` through outbox `push.requested` events.
Driver scope only: Shipper sessions never read these tables and drivers read only their own rows.
"""
from alembic import op

revision = '0034_driver_push'
down_revision = '0033_order_chat'
branch_labels = None
depends_on = None

DRIVER = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = ''
    AND (coalesce(current_setting('app.driver_id', true), '') = '' OR driver_id::text = current_setting('app.driver_id', true))"""


def upgrade():
    op.execute("""CREATE TABLE push_devices (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        user_id uuid NOT NULL REFERENCES users(id),
        driver_id uuid NOT NULL,
        session_hash varchar(64) NOT NULL REFERENCES login_sessions(token_hash) ON DELETE CASCADE,
        platform varchar(10) NOT NULL,
        token varchar(4096) NOT NULL,
        created_at timestamptz NOT NULL,
        last_seen_at timestamptz NOT NULL,
        CONSTRAINT push_device_platform CHECK (platform IN ('ANDROID','IOS')),
        CONSTRAINT push_devices_token UNIQUE (organization_id, token),
        FOREIGN KEY (organization_id, driver_id) REFERENCES drivers(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_push_devices_user ON push_devices (organization_id, user_id)')
    op.execute("""CREATE TABLE push_messages (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        user_id uuid NOT NULL REFERENCES users(id),
        driver_id uuid NOT NULL,
        kind varchar(80) NOT NULL,
        title varchar(160) NOT NULL,
        body varchar(500) NOT NULL,
        data jsonb NOT NULL,
        created_at timestamptz NOT NULL,
        sent_at timestamptz,
        FOREIGN KEY (organization_id, driver_id) REFERENCES drivers(organization_id, id)
    )""")
    for table in ('push_devices', 'push_messages'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({DRIVER}) WITH CHECK ({DRIVER})')
    op.execute('GRANT SELECT, INSERT, DELETE ON push_devices TO dispatra_app')
    op.execute('GRANT UPDATE (user_id, driver_id, session_hash, platform, last_seen_at) ON push_devices TO dispatra_app')
    op.execute('GRANT SELECT, INSERT ON push_messages TO dispatra_app')
    op.execute('GRANT UPDATE (sent_at) ON push_messages TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE push_messages')
    op.execute('DROP TABLE push_devices')
