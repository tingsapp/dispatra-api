"""Owner connector controls and company setup requests; preserve saved connections."""
from alembic import op

revision = '0055_connector_management'
down_revision = '0054_trial_settings'
branch_labels = None
depends_on = None

OWNER = "current_setting('app.platform', true) = 'true'"
COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""

def upgrade():
    op.execute('ALTER TABLE connector_connections ADD COLUMN paused boolean NOT NULL DEFAULT false')
    op.execute('''CREATE TABLE connector_providers (id uuid PRIMARY KEY, provider varchar(30) NOT NULL UNIQUE,
      enabled boolean NOT NULL, version integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now())''')
    names = ['rose-rocket','cartoncloud','truckmate','mcleod','aljex','turvo','truckbase','tailwind','ascendtms']
    for index, name in enumerate(names, 1):
        enabled = 'true' if index <= 6 else 'false'
        op.execute(f"INSERT INTO connector_providers(id,provider,enabled) VALUES ('d5150000-0000-4000-8000-{index:012d}','{name}',{enabled})")
    op.execute('ALTER TABLE connector_providers ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE connector_providers FORCE ROW LEVEL SECURITY')
    # Catalogue availability has no secrets; companies may read but cannot change it.
    op.execute('CREATE POLICY catalogue_read ON connector_providers FOR SELECT USING (true)')
    op.execute(f'CREATE POLICY owner_update ON connector_providers FOR UPDATE USING ({OWNER}) WITH CHECK ({OWNER})')
    op.execute('GRANT SELECT, UPDATE ON connector_providers TO dispatra_app')
    op.execute('''CREATE TABLE connector_setup_requests (id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id),
      provider varchar(30) NOT NULL, status varchar(20) NOT NULL CHECK(status IN ('REQUESTED','IN_PROGRESS','COMPLETED','CANCELLED')),
      version integer NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
      UNIQUE(organization_id,id), UNIQUE(organization_id,provider))''')
    op.execute('ALTER TABLE connector_setup_requests ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE connector_setup_requests FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY company_read ON connector_setup_requests FOR SELECT USING ({COMPANY})')
    op.execute(f'CREATE POLICY company_request ON connector_setup_requests FOR INSERT WITH CHECK ({COMPANY})')
    op.execute(f'CREATE POLICY owner_manage ON connector_setup_requests FOR UPDATE USING ({OWNER}) WITH CHECK ({OWNER})')
    op.execute('GRANT SELECT, INSERT, UPDATE ON connector_setup_requests TO dispatra_app')
    op.execute('CREATE INDEX ix_connector_setup_requests_company ON connector_setup_requests(organization_id)')

def downgrade():
    op.execute('DROP TABLE connector_setup_requests')
    op.execute('DROP TABLE connector_providers')
    op.execute('ALTER TABLE connector_connections DROP COLUMN paused')
