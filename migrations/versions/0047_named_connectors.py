"""Independent named connectors; leaves manual TMS tables and settings unchanged."""
from alembic import op
revision='0047_named_connectors'
down_revision='0046_company_subscriptions'
branch_labels=None
depends_on=None

COMPANY="""(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
 AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""
COMMON='id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id), version integer NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, UNIQUE(organization_id,id)'

def upgrade():
    op.execute(f'''CREATE TABLE connector_connections ({COMMON}, provider varchar(30) NOT NULL,
      status varchar(30) NOT NULL CHECK(status IN ('CONNECTING','CONNECTED','SYNCING','RECONNECT_REQUIRED','SYNC_FAILED','DISCONNECTED')),
      credentials text, account_id varchar(200), account_label varchar(160), profile_revision varchar(80),
      last_sync_at timestamptz, last_attempt_at timestamptz,last_error varchar(80),summary jsonb NOT NULL,
      lease uuid, lease_until timestamptz,next_attempt_at timestamptz,event_cursor varchar(42), UNIQUE(organization_id,provider),
      CHECK(provider IN ('rose-rocket','cartoncloud','truckmate','mcleod','aljex','turvo')))''')
    op.execute(f'''CREATE TABLE connector_authorizations ({COMMON},provider varchar(30) NOT NULL,user_id uuid NOT NULL REFERENCES users(id),
      state_hash varchar(64) NOT NULL UNIQUE,verifier text NOT NULL,connection_version integer,profile_revision varchar(80) NOT NULL,
      expires_at timestamptz NOT NULL,used boolean NOT NULL)''')
    op.execute(f'''CREATE TABLE connector_links ({COMMON},connection_id uuid NOT NULL,kind varchar(10) NOT NULL,
      external_id varchar(200) NOT NULL,record_id uuid NOT NULL,fingerprint varchar(64),UNIQUE(connection_id,kind,external_id),
      CHECK(kind IN ('DRIVER','VEHICLE','SHIPPER','ORDER')),
      FOREIGN KEY(organization_id,connection_id) REFERENCES connector_connections(organization_id,id))''')
    op.execute(f'''CREATE TABLE connector_inbox ({COMMON},connection_id uuid NOT NULL,external_id varchar(200) NOT NULL,
      revision varchar(64) NOT NULL,profile_revision varchar(80) NOT NULL,kind varchar(10) NOT NULL CHECK(kind IN ('DRIVER','VEHICLE','ORDER')),payload jsonb NOT NULL,status varchar(20) NOT NULL,
      reasons jsonb NOT NULL,order_id uuid,UNIQUE(connection_id,kind,external_id,revision),
      CHECK(status IN ('RECEIVED','IMPORTED','NEEDS_REVIEW','IGNORED')),
      FOREIGN KEY(organization_id,connection_id) REFERENCES connector_connections(organization_id,id),
      FOREIGN KEY(organization_id,order_id) REFERENCES orders(organization_id,id))''')
    for table in ['connector_connections','connector_authorizations','connector_links','connector_inbox']:
        op.execute(f'CREATE INDEX ix_{table}_company ON {table}(organization_id)')
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({COMPANY}) WITH CHECK ({COMPANY})')
        op.execute(f'GRANT SELECT,INSERT,UPDATE ON {table} TO dispatra_app')
    op.execute('CREATE INDEX ix_connector_connections_due ON connector_connections(next_attempt_at) WHERE credentials IS NOT NULL')

def downgrade():
    for table in ['connector_inbox','connector_links','connector_authorizations','connector_connections']:op.execute(f'DROP TABLE {table}')
