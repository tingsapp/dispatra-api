"""Versioned company/dispatcher administration, access metadata and a single platform owner."""
from alembic import op
revision = '0014_platform_administration'
down_revision = '0013_stripe_card_setup'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE organizations ADD COLUMN version integer NOT NULL DEFAULT 1')
    op.execute('ALTER TABLE organizations ADD COLUMN updated_at timestamptz')
    op.execute('UPDATE organizations SET updated_at = created_at')
    op.execute('ALTER TABLE organizations ALTER COLUMN updated_at SET NOT NULL')
    op.execute('ALTER TABLE organizations ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP')
    op.execute('ALTER TABLE users ADD COLUMN version integer NOT NULL DEFAULT 1')
    op.execute("ALTER TABLE users ADD COLUMN display_name varchar(160) NOT NULL DEFAULT ''")
    op.execute('ALTER TABLE users ADD COLUMN updated_at timestamptz')
    op.execute('UPDATE users SET updated_at = created_at')
    op.execute('ALTER TABLE users ALTER COLUMN updated_at SET NOT NULL')
    op.execute('ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP')
    op.execute('ALTER TABLE users ADD COLUMN last_login_at timestamptz')
    # Exactly one bootstrap-provisioned platform owner; company roles are unaffected.
    op.execute("CREATE UNIQUE INDEX users_single_platform_owner ON users ((true)) WHERE role = 'PLATFORM_OWNER'")
    op.execute('CREATE INDEX ix_audit_events_org_created ON audit_events (organization_id, created_at DESC)')
    op.execute('CREATE INDEX ix_organizations_name ON organizations (lower(name), id)')


def downgrade():
    op.execute('DROP INDEX ix_organizations_name')
    op.execute('DROP INDEX ix_audit_events_org_created')
    op.execute('DROP INDEX users_single_platform_owner')
    for column in ['last_login_at', 'updated_at', 'display_name', 'version']:
        op.execute(f'ALTER TABLE users DROP COLUMN {column}')
    for column in ['updated_at', 'version']:
        op.execute(f'ALTER TABLE organizations DROP COLUMN {column}')
