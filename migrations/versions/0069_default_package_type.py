"""Give every company a Default Package Type for email intake: its active Parcel type, without replacing a saved choice."""
from alembic import op

revision = '0069_default_package_type'
down_revision = '0068_package_types'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE company_settings s SET data = s.data || jsonb_build_object('default_package_type_id', c.id::text)
        FROM catalog_entries c
        WHERE c.organization_id = s.organization_id AND c.kind = 'PACKAGE_TYPE' AND c.code = 'PARCEL' AND c.active
          AND coalesce(s.data->>'default_package_type_id', '') = ''""")


def downgrade():
    op.execute("UPDATE company_settings SET data = data - 'default_package_type_id'")
