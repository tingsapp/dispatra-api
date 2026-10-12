"""Limit companies to one active dispatcher, retaining historical accounts and audit links."""
from alembic import op

revision = '0036_single_dispatcher'
down_revision = '0035_imported_rate_card'
branch_labels = None
depends_on = None


def upgrade():
    # Keep the oldest active account. Already inactive accounts stay inactive.
    # Sessions belonging to additional accounts must end immediately.
    op.execute("""WITH ranked AS (
        SELECT id, row_number() OVER (PARTITION BY organization_id ORDER BY created_at, id) AS position
        FROM users WHERE role = 'DISPATCHER' AND active
    ), disabled AS (
        UPDATE users SET active = false, version = version + 1, updated_at = now()
        WHERE id IN (SELECT id FROM ranked WHERE position > 1) RETURNING id, organization_id
    ), revoked AS (
        DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM disabled) RETURNING user_id
    ) INSERT INTO audit_events (id, organization_id, actor_id, action, entity_id, created_at)
        SELECT gen_random_uuid(), organization_id, (SELECT id FROM users WHERE role = 'ADMIN' LIMIT 1),
               'dispatcher.deactivated', id, now() FROM disabled""")
    op.execute("""CREATE UNIQUE INDEX one_active_company_dispatcher
        ON users (organization_id) WHERE role = 'DISPATCHER' AND active""")


def downgrade():
    op.execute('DROP INDEX one_active_company_dispatcher')
    # Do not restore sessions or silently reactivate accounts.
