"""Permit Samsara connections while preserving existing provider records and controls."""
from alembic import op
revision='0058_samsara_connection'
down_revision='0057_company_shipper_chat'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE connector_connections DROP CONSTRAINT connector_connections_provider_check')
    op.execute("ALTER TABLE connector_connections ADD CONSTRAINT connector_connections_provider_check CHECK(provider IN ('rose-rocket','cartoncloud','truckmate','mcleod','aljex','turvo','samsara'))")
    op.execute("INSERT INTO connector_providers(id,provider,enabled,version,updated_at) VALUES ('d5150000-0000-4000-8000-000000000010','samsara',true,1,now()) ON CONFLICT(provider) DO NOTHING")


def downgrade():
    # A downgrade refuses existing Samsara connections instead of deleting operational records.
    op.execute('ALTER TABLE connector_connections DROP CONSTRAINT connector_connections_provider_check')
    op.execute("ALTER TABLE connector_connections ADD CONSTRAINT connector_connections_provider_check CHECK(provider IN ('rose-rocket','cartoncloud','truckmate','mcleod','aljex','turvo'))")
