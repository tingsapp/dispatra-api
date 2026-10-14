"""Remember the Dispatra pricing default across external TMS mode changes."""
from alembic import op
revision = '0048_workspace_rate'
down_revision = '0047_named_connectors'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE company_settings ADD COLUMN native_rate_card_id uuid')
    op.execute('ALTER TABLE company_settings ADD CONSTRAINT settings_native_rate_company '
        'FOREIGN KEY (organization_id, native_rate_card_id) REFERENCES rate_cards(organization_id, id)')


def downgrade():
    op.execute('ALTER TABLE company_settings DROP CONSTRAINT settings_native_rate_company')
    op.execute('ALTER TABLE company_settings DROP COLUMN native_rate_card_id')
