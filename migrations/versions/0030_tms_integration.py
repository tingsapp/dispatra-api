"""Connected TMS: one encrypted company connection, links from TMS identifiers to imported records, and an order inbox.

All tables are company-dispatcher scope only: Shipper and driver sessions never read them.
"""
from alembic import op

revision = '0030_tms_integration'
down_revision = '0029_remove_payment_terms'
branch_labels = None
depends_on = None

COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    op.execute("""CREATE TABLE tms_connections (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL UNIQUE REFERENCES organizations(id),
        enabled boolean NOT NULL,
        base_url varchar(2048) NOT NULL,
        auth_type varchar(10) NOT NULL,
        credential text NOT NULL,
        drivers_endpoint varchar(500) NOT NULL,
        vehicles_endpoint varchar(500) NOT NULL,
        fleet_sync_minutes integer NOT NULL,
        sync_method varchar(10) NOT NULL,
        webhook_secret text,
        orders_endpoint varchar(500) NOT NULL,
        poll_interval_minutes integer NOT NULL,
        status_endpoint varchar(500) NOT NULL,
        field_maps jsonb NOT NULL,
        poll_cursor varchar(200),
        last_fleet_sync_at timestamptz,
        last_fleet_result jsonb,
        last_order_poll_at timestamptz,
        last_error varchar(80),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT tms_auth_type CHECK (auth_type IN ('api_key','bearer')),
        CONSTRAINT tms_sync_method CHECK (sync_method IN ('webhook','polling')),
        CONSTRAINT tms_fleet_interval CHECK (fleet_sync_minutes IN (15, 60, 360, 1440)),
        CONSTRAINT tms_poll_interval CHECK (poll_interval_minutes IN (1, 5, 15)),
        CONSTRAINT tms_connections_organization_id_id_key UNIQUE (organization_id, id)
    )""")
    op.execute("""CREATE TABLE tms_links (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        kind varchar(10) NOT NULL,
        external_id varchar(200) NOT NULL,
        record_id uuid NOT NULL,
        fingerprint varchar(64),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT tms_link_kind CHECK (kind IN ('DRIVER','VEHICLE','SHIPPER','ORDER')),
        CONSTRAINT tms_links_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT tms_links_external UNIQUE (organization_id, kind, external_id),
        CONSTRAINT tms_links_record UNIQUE (organization_id, kind, record_id)
    )""")
    op.execute("""CREATE TABLE tms_inbox (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        external_id varchar(200) NOT NULL,
        revision varchar(200) NOT NULL,
        event varchar(40) NOT NULL,
        received_via varchar(10) NOT NULL,
        received_at timestamptz NOT NULL,
        payload jsonb NOT NULL,
        status varchar(20) NOT NULL,
        missing jsonb NOT NULL,
        order_id uuid,
        error_code varchar(80),
        attempts integer NOT NULL,
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT tms_inbox_status CHECK (status IN ('RECEIVED','ORDER_CREATED','ORDER_UPDATED','ORDER_CANCELLED','NEEDS_REVIEW','FAILED','IGNORED')),
        CONSTRAINT tms_inbox_via CHECK (received_via IN ('WEBHOOK','POLLING')),
        CONSTRAINT tms_inbox_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT tms_inbox_revision UNIQUE (organization_id, external_id, revision),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_tms_inbox_queue ON tms_inbox (organization_id, status, received_at)')
    for table in ['tms_connections', 'tms_links', 'tms_inbox']:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({COMPANY}) WITH CHECK ({COMPANY})')
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO dispatra_app')


def downgrade():
    for table in ['tms_inbox', 'tms_links', 'tms_connections']: op.execute(f'DROP TABLE {table}')
