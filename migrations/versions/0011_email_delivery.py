"""Durable, tenant-scoped quote and invoice email requests."""
from alembic import op

revision = '0011_email_delivery'
down_revision = '0010_directory_archival'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE email_deliveries (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        quote_id uuid,
        invoice_id uuid,
        requested_by uuid NOT NULL REFERENCES users(id),
        recipient varchar(254) NOT NULL,
        subject varchar(300) NOT NULL,
        body_text text NOT NULL,
        body_html text NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'PENDING',
        message_id varchar(200) NOT NULL UNIQUE,
        sent_at timestamptz,
        error_code varchar(80),
        CONSTRAINT email_delivery_source CHECK ((quote_id IS NULL) <> (invoice_id IS NULL)),
        CONSTRAINT email_delivery_status CHECK (status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN')),
        CONSTRAINT email_delivery_quote_fk FOREIGN KEY (organization_id, quote_id) REFERENCES quotes(organization_id,id),
        CONSTRAINT email_delivery_invoice_fk FOREIGN KEY (organization_id, invoice_id) REFERENCES invoices(organization_id,id),
        CONSTRAINT email_delivery_tenant_id UNIQUE (organization_id,id)
    )""")
    op.execute("CREATE INDEX ix_email_deliveries_company_status ON email_deliveries (organization_id,status,created_at,id)")
    op.execute("CREATE UNIQUE INDEX uq_email_pending_quote ON email_deliveries (organization_id,quote_id,recipient) WHERE quote_id IS NOT NULL AND status IN ('PENDING','SENDING')")
    op.execute("CREATE UNIQUE INDEX uq_email_pending_invoice ON email_deliveries (organization_id,invoice_id,recipient) WHERE invoice_id IS NOT NULL AND status IN ('PENDING','SENDING')")
    op.execute('ALTER TABLE email_deliveries ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE email_deliveries FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY tenant_isolation ON email_deliveries
        USING ((organization_id::text = nullif(current_setting('app.organization_id', true), '')
            OR current_setting('app.platform', true) = 'true')
            AND coalesce(current_setting('app.customer_id', true), '') = ''
            AND coalesce(current_setting('app.driver_id', true), '') = '')
        WITH CHECK ((organization_id::text = nullif(current_setting('app.organization_id', true), '')
            OR current_setting('app.platform', true) = 'true')
            AND coalesce(current_setting('app.customer_id', true), '') = ''
            AND coalesce(current_setting('app.driver_id', true), '') = '')""")
    op.execute('GRANT SELECT, INSERT, UPDATE ON email_deliveries TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE email_deliveries')
