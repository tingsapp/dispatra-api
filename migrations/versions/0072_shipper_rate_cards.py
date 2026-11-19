"""Shippers can carry several Rate Cards. `shipper_rate_cards` keeps them in the dispatcher's order; position 0 stays in
`shippers.rate_card_id`, which still prices the Shipper's Orders. Rows follow the Shipper's own row-level visibility."""
from alembic import op

revision = '0072_shipper_rate_cards'
down_revision = '0071_order_archive'
branch_labels = None
depends_on = None

TENANT = "organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true'"


def upgrade():
    op.execute("""CREATE TABLE shipper_rate_cards (
        organization_id uuid NOT NULL REFERENCES organizations(id),
        shipper_id uuid NOT NULL,
        rate_card_id uuid NOT NULL,
        position integer NOT NULL CHECK (position >= 0),
        PRIMARY KEY (shipper_id, rate_card_id),
        CONSTRAINT shipper_rate_cards_shipper_id_position_key UNIQUE (shipper_id, position),
        FOREIGN KEY (organization_id, shipper_id) REFERENCES shippers(organization_id, id) ON DELETE CASCADE,
        FOREIGN KEY (organization_id, rate_card_id) REFERENCES rate_cards(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_shipper_rate_cards_organization_id ON shipper_rate_cards (organization_id)')
    op.execute('CREATE INDEX ix_shipper_rate_cards_rate_card ON shipper_rate_cards (organization_id, rate_card_id)')
    op.execute("""INSERT INTO shipper_rate_cards (organization_id, shipper_id, rate_card_id, position)
        SELECT organization_id, id, rate_card_id, 0 FROM shippers WHERE rate_card_id IS NOT NULL""")
    op.execute('ALTER TABLE shipper_rate_cards ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE shipper_rate_cards FORCE ROW LEVEL SECURITY')
    expression = f'({TENANT}) AND shipper_id IN (SELECT id FROM shippers)'
    op.execute(f'CREATE POLICY tenant_isolation ON shipper_rate_cards USING ({expression}) WITH CHECK ({expression})')
    op.execute('GRANT SELECT, INSERT, DELETE ON shipper_rate_cards TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE shipper_rate_cards')
