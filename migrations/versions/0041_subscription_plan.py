"""One owner-managed monthly SaaS plan, with no fabricated price or payments."""
from alembic import op

revision = '0041_subscription_plan'
down_revision = '0040_order_watch'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE subscription_plans (
        id uuid PRIMARY KEY CHECK (id = '8eb730cb-226b-4ec1-a3fc-132279938d62'),
        monthly_price numeric(12,2) CHECK (monthly_price >= 0),
        currency varchar(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
        version integer NOT NULL CHECK (version >= 1),
        updated_at timestamptz NOT NULL
    )""")
    op.execute("INSERT INTO subscription_plans (id, monthly_price, currency, version, updated_at) VALUES ('8eb730cb-226b-4ec1-a3fc-132279938d62', NULL, 'CAD', 1, now())")
    op.execute('ALTER TABLE subscription_plans ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE subscription_plans FORCE ROW LEVEL SECURITY')
    owner = "current_setting('app.platform',true) = 'true' AND coalesce(current_setting('app.organization_id',true),'') = '' AND coalesce(current_setting('app.shipper_id',true),'') = '' AND coalesce(current_setting('app.driver_id',true),'') = ''"
    op.execute(f'CREATE POLICY owner_scope ON subscription_plans USING ({owner}) WITH CHECK ({owner})')
    op.execute('GRANT SELECT,UPDATE ON subscription_plans TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE subscription_plans')
