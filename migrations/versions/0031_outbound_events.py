"""Outbound order events: status delivery state on the TMS connection and one signed company webhook for the Dispatra API.

Delivery positions live in `event_subscriptions` (consumers `tms-status` and `api-webhooks`). Company-dispatcher scope only.
"""
from alembic import op

revision = '0031_outbound_events'
down_revision = '0030_tms_integration'
branch_labels = None
depends_on = None

COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    # Polling deduplicates by payload hash, so the cursor added in 0030 is not used.
    op.execute('ALTER TABLE tms_connections ADD COLUMN last_status_at timestamptz, ADD COLUMN last_status_error varchar(80), DROP COLUMN poll_cursor')
    op.execute("""CREATE TABLE api_webhooks (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL UNIQUE REFERENCES organizations(id),
        url varchar(2048) NOT NULL,
        secret text NOT NULL,
        enabled boolean NOT NULL,
        last_delivery_at timestamptz,
        last_error varchar(80),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT api_webhooks_organization_id_id_key UNIQUE (organization_id, id)
    )""")
    op.execute('ALTER TABLE api_webhooks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE api_webhooks FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON api_webhooks USING ({COMPANY}) WITH CHECK ({COMPANY})')
    op.execute('GRANT SELECT, INSERT, UPDATE ON api_webhooks TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE api_webhooks')
    op.execute('ALTER TABLE tms_connections DROP COLUMN last_status_error, DROP COLUMN last_status_at, ADD COLUMN poll_cursor varchar(200)')
