"""Dispatra API keys: one active key per company, stored only as a SHA-256 hash of the 256-bit secret.

Company-dispatcher scope only. Authentication looks keys up under platform context by hash.
"""
from alembic import op

revision = '0032_api_keys'
down_revision = '0031_outbound_events'
branch_labels = None
depends_on = None

COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    op.execute("""CREATE TABLE api_keys (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        key_hash varchar(64) NOT NULL UNIQUE,
        prefix varchar(16) NOT NULL,
        last_used_at timestamptz,
        revoked_at timestamptz,
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT api_keys_organization_id_id_key UNIQUE (organization_id, id)
    )""")
    op.execute('CREATE UNIQUE INDEX api_keys_one_active ON api_keys (organization_id) WHERE revoked_at IS NULL')
    op.execute('ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE api_keys FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON api_keys USING ({COMPANY}) WITH CHECK ({COMPANY})')
    op.execute('GRANT SELECT, INSERT, UPDATE ON api_keys TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE api_keys')
