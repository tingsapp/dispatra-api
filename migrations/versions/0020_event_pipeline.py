"""Company event log, per-account notification inbox and server consumer positions.

Driver inbox rows move into `notifications`; `/driver/notifications` reads them unchanged.
Events carry their writing transaction (xid8) so feeds never skip a later-committing lower sequence.
"""
from alembic import op

revision = '0020_event_pipeline'
down_revision = '0019_driver_notifications'
branch_labels = None
depends_on = None

TENANT = "(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')"
SCOPED = TENANT + """
    AND (coalesce(current_setting('app.shipper_id', true), '') = '' OR shipper_id::text = current_setting('app.shipper_id', true))
    AND (coalesce(current_setting('app.driver_id', true), '') = '' OR driver_id::text = current_setting('app.driver_id', true))"""


def upgrade():
    op.execute("""CREATE TABLE events (
        seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        id uuid NOT NULL UNIQUE,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        tx xid8 NOT NULL DEFAULT pg_current_xact_id(),
        type varchar(80) NOT NULL,
        actor_type varchar(10) NOT NULL,
        actor_id uuid,
        entity varchar(40) NOT NULL,
        entity_id uuid NOT NULL,
        entity_version integer,
        order_id uuid,
        route_id uuid,
        dispatchers boolean NOT NULL,
        shipper_id uuid,
        driver_id uuid,
        user_id uuid,
        correlation_id uuid NOT NULL,
        causation_id uuid,
        created_at timestamptz NOT NULL,
        CONSTRAINT event_actor_type CHECK (actor_type IN ('USER','SYSTEM','AGENT')),
        CONSTRAINT events_organization_id_id_key UNIQUE (organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_events_feed ON events (organization_id, tx, seq)')
    op.execute("""CREATE TABLE notifications (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        recipient_user_id uuid NOT NULL REFERENCES users(id),
        shipper_id uuid,
        driver_id uuid,
        event_id uuid,
        kind varchar(80) NOT NULL,
        severity varchar(10) NOT NULL,
        order_id uuid,
        route_id uuid,
        title varchar(160) NOT NULL,
        body varchar(500) NOT NULL,
        dedupe_key varchar(200),
        read_at timestamptz,
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT notification_severity CHECK (severity IN ('INFO','WARNING','CRITICAL')),
        CONSTRAINT notifications_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT notifications_dedupe UNIQUE (organization_id, recipient_user_id, dedupe_key),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id),
        FOREIGN KEY (organization_id, route_id) REFERENCES routes(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_notifications_inbox ON notifications (organization_id, recipient_user_id, created_at DESC, id DESC)')
    op.execute('CREATE INDEX ix_notifications_unread ON notifications (organization_id, recipient_user_id) WHERE read_at IS NULL')
    # Copy before RLS is enabled; the old table's policy does not admit the migration owner.
    op.execute('ALTER TABLE driver_notifications NO FORCE ROW LEVEL SECURITY')
    op.execute("""INSERT INTO notifications (id, organization_id, recipient_user_id, driver_id, kind, severity, order_id, route_id,
            title, body, read_at, version, created_at, updated_at)
        SELECT n.id, n.organization_id, u.id, n.driver_id,
            CASE WHEN n.order_id IS NULL THEN 'route.released' ELSE 'order.assigned' END, 'INFO',
            n.order_id, n.route_id, n.title, n.body, n.read_at, n.version, n.created_at, n.updated_at
        FROM driver_notifications n JOIN users u ON u.organization_id = n.organization_id AND u.driver_id = n.driver_id""")
    op.execute('DROP TABLE driver_notifications')
    op.execute("""CREATE TABLE event_subscriptions (
        organization_id uuid NOT NULL REFERENCES organizations(id),
        consumer varchar(80) NOT NULL,
        position varchar(42) NOT NULL,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (organization_id, consumer)
    )""")
    for table, predicate in [('events', SCOPED), ('notifications', SCOPED), ('event_subscriptions', TENANT
            + " AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''")]:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})')
    op.execute('GRANT SELECT, INSERT ON events TO dispatra_app')
    op.execute('GRANT SELECT, INSERT ON notifications TO dispatra_app')
    op.execute('GRANT UPDATE (read_at, version, updated_at) ON notifications TO dispatra_app')
    op.execute('GRANT SELECT, INSERT, UPDATE ON event_subscriptions TO dispatra_app')


def downgrade():
    op.execute("""CREATE TABLE driver_notifications (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        driver_id uuid NOT NULL,
        order_id uuid,
        route_id uuid,
        title varchar(160) NOT NULL,
        body varchar(500) NOT NULL,
        read_at timestamptz,
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        UNIQUE (organization_id, id),
        FOREIGN KEY (organization_id, driver_id) REFERENCES drivers(organization_id, id),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id),
        FOREIGN KEY (organization_id, route_id) REFERENCES routes(organization_id, id)
    )""")
    op.execute('ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY')
    op.execute("""INSERT INTO driver_notifications (id, organization_id, driver_id, order_id, route_id, title, body, read_at, version, created_at, updated_at)
        SELECT id, organization_id, driver_id, order_id, route_id, title, body, read_at, version, created_at, updated_at
        FROM notifications WHERE driver_id IS NOT NULL AND kind IN ('order.assigned', 'route.released')""")
    op.execute('CREATE INDEX ix_driver_notifications_inbox ON driver_notifications (organization_id, driver_id, created_at)')
    op.execute('ALTER TABLE driver_notifications ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE driver_notifications FORCE ROW LEVEL SECURITY')
    scope = """organization_id::text = nullif(current_setting('app.organization_id', true), '')
        AND coalesce(current_setting('app.shipper_id', true), '') = ''
        AND (coalesce(current_setting('app.driver_id', true), '') = '' OR driver_id::text = current_setting('app.driver_id', true))"""
    op.execute(f'CREATE POLICY tenant_isolation ON driver_notifications USING ({scope}) WITH CHECK ({scope})')
    op.execute('GRANT SELECT, INSERT ON driver_notifications TO dispatra_app')
    op.execute('GRANT UPDATE (read_at, version, updated_at) ON driver_notifications TO dispatra_app')
    op.execute('DROP TABLE event_subscriptions')
    op.execute('DROP TABLE notifications')
    op.execute('DROP TABLE events')
