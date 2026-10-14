"""Independent company SaaS agreements; workspace access never implies payment."""
from alembic import op

revision = '0046_company_subscriptions'
down_revision = '0045_verification_credentials'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE company_subscriptions (
        organization_id uuid PRIMARY KEY REFERENCES organizations(id),
        status varchar(20) NOT NULL CHECK (status IN ('NOT_SUBSCRIBED','ACTIVE','PAUSED','CANCELLED')),
        monthly_price numeric(12,2) CHECK (monthly_price >= 0),
        currency varchar(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
        starts_on date,
        version integer NOT NULL CHECK (version >= 1),
        updated_at timestamptz NOT NULL,
        CHECK (status <> 'ACTIVE' OR (monthly_price IS NOT NULL AND starts_on IS NOT NULL))
    )""")
    op.execute("""INSERT INTO company_subscriptions
        SELECT o.id, 'NOT_SUBSCRIBED', p.monthly_price, p.currency, NULL, 1, now()
        FROM organizations o CROSS JOIN subscription_plans p""")
    op.execute('ALTER TABLE company_subscriptions ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE company_subscriptions FORCE ROW LEVEL SECURITY')
    owner = "current_setting('app.platform',true) = 'true' AND coalesce(current_setting('app.organization_id',true),'') = '' AND coalesce(current_setting('app.shipper_id',true),'') = '' AND coalesce(current_setting('app.driver_id',true),'') = ''"
    op.execute(f'CREATE POLICY owner_scope ON company_subscriptions USING ({owner}) WITH CHECK ({owner})')
    # Trial provisioning may insert only an initial non-revenue record in its new tenant.
    op.execute("""CREATE POLICY initial_subscription ON company_subscriptions FOR INSERT WITH CHECK (
        organization_id::text = current_setting('app.organization_id',true)
        AND status = 'NOT_SUBSCRIBED' AND version = 1 AND starts_on IS NULL
        AND coalesce(current_setting('app.shipper_id',true),'') = ''
        AND coalesce(current_setting('app.driver_id',true),'') = '')""")
    op.execute('GRANT SELECT,INSERT,UPDATE ON company_subscriptions TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE company_subscriptions')
