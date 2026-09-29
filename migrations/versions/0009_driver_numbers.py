"""Server-issued, organization-unique Driver numbers for the existing list."""
from alembic import op

revision = '0009_driver_numbers'
down_revision = '0008_outbox_delivery'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE drivers ADD COLUMN number varchar(30)')
    op.execute("""WITH numbered AS (
        SELECT id, row_number() OVER (PARTITION BY organization_id ORDER BY created_at, id) AS ordinal
        FROM drivers
    )
    UPDATE drivers AS d SET number = 'D' || lpad(numbered.ordinal::text, 2, '0')
    FROM numbered WHERE numbered.id = d.id""")
    op.execute('ALTER TABLE drivers ALTER COLUMN number SET NOT NULL')
    op.execute('ALTER TABLE drivers ADD CONSTRAINT drivers_company_number UNIQUE (organization_id, number)')


def downgrade():
    op.execute('ALTER TABLE drivers DROP CONSTRAINT drivers_company_number')
    op.execute('ALTER TABLE drivers DROP COLUMN number')
