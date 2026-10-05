"""Order agent email intake: one encrypted company mailbox connection and one intake record per received message.

Both tables are company-dispatcher scope only: Shipper and driver sessions never read them.
"""
from alembic import op

revision = '0022_email_intake'
down_revision = '0021_remove_stripe'
branch_labels = None
depends_on = None

COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    op.execute("""CREATE TABLE mailbox_connections (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL UNIQUE REFERENCES organizations(id),
        host varchar(253) NOT NULL,
        port integer NOT NULL,
        username varchar(254) NOT NULL,
        secret text NOT NULL,
        folder varchar(120) NOT NULL,
        enabled boolean NOT NULL,
        default_service_id uuid,
        uid_validity bigint,
        last_uid bigint NOT NULL DEFAULT 0,
        last_polled_at timestamptz,
        last_error varchar(80),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT mailbox_port CHECK (port BETWEEN 1 AND 65535),
        CONSTRAINT mailbox_connections_organization_id_id_key UNIQUE (organization_id, id),
        FOREIGN KEY (organization_id, default_service_id) REFERENCES catalog_entries(organization_id, id)
    )""")
    op.execute("""CREATE TABLE email_intakes (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        message_id varchar(998) NOT NULL,
        mailbox_uid bigint,
        received_at timestamptz NOT NULL,
        from_address varchar(254) NOT NULL,
        from_name varchar(160) NOT NULL,
        subject varchar(500) NOT NULL,
        body text NOT NULL,
        sender_verified boolean NOT NULL,
        shipper_id uuid,
        status varchar(20) NOT NULL,
        extraction jsonb,
        draft jsonb,
        missing jsonb NOT NULL,
        summary varchar(500) NOT NULL,
        order_id uuid,
        error_code varchar(80),
        attempts integer NOT NULL,
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT email_intake_status CHECK (status IN ('RECEIVED','ORDER_CREATED','NEEDS_REVIEW','UNKNOWN_SENDER','NOT_AN_ORDER','FAILED','DISCARDED')),
        CONSTRAINT email_intakes_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT email_intakes_message UNIQUE (organization_id, message_id),
        FOREIGN KEY (organization_id, shipper_id) REFERENCES shippers(organization_id, id),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_email_intakes_queue ON email_intakes (organization_id, status, received_at DESC)')
    for table in ['mailbox_connections', 'email_intakes']:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({COMPANY}) WITH CHECK ({COMPANY})')
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE email_intakes')
    op.execute('DROP TABLE mailbox_connections')
