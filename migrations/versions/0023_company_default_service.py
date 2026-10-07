"""The default service level is a company setting, not a mailbox setting: move any mailbox choice into company settings."""
from alembic import op

revision = '0023_company_default_service'
down_revision = '0022_email_intake'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE company_settings s SET data = s.data || jsonb_build_object('default_service_id', m.default_service_id::text)
        FROM mailbox_connections m, catalog_entries c
        WHERE m.organization_id = s.organization_id AND m.default_service_id IS NOT NULL AND coalesce(s.data->>'default_service_id', '') = ''
          AND c.organization_id = m.organization_id AND c.id = m.default_service_id AND c.kind = 'SERVICE' AND c.active""")
    # Companies without a choice get the one new orders already started on: Same-Day Standard, else the first no-charge service.
    op.execute("""UPDATE company_settings s SET data = s.data || jsonb_build_object('default_service_id', (
            SELECT c.id::text FROM catalog_entries c WHERE c.organization_id = s.organization_id AND c.kind = 'SERVICE' AND c.active
            ORDER BY lower(c.data->>'name') = 'same-day standard' DESC, (c.data->>'amount')::numeric = 0 DESC, c.created_at, c.code LIMIT 1))
        WHERE coalesce(s.data->>'default_service_id', '') = ''""")
    op.execute('ALTER TABLE mailbox_connections DROP COLUMN default_service_id')


def downgrade():
    op.execute('ALTER TABLE mailbox_connections ADD COLUMN default_service_id uuid')
    op.execute("""ALTER TABLE mailbox_connections ADD CONSTRAINT mailbox_connections_organization_id_default_service_id_fkey
        FOREIGN KEY (organization_id, default_service_id) REFERENCES catalog_entries(organization_id, id)""")
