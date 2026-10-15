"""Durable company SaaS billing; existing saved cards require fresh recurring consent."""
from alembic import op
revision = '0052_recurring_company_billing'
down_revision = '0051_shipper_assigned_driver'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE organizations ADD COLUMN billing_enrolled boolean NOT NULL DEFAULT false, ADD COLUMN billing_access_until timestamptz")
    op.execute("""ALTER TABLE company_billing
        ADD COLUMN consented_at timestamptz, ADD COLUMN consent_terms jsonb, ADD COLUMN setup_terms_version integer,
        ADD COLUMN stripe_subscription varchar(255) UNIQUE, ADD COLUMN subscription_status varchar(40),
        ADD COLUMN period_end timestamptz, ADD COLUMN paid_until timestamptz, ADD COLUMN invoice_url varchar(2048),
        ADD COLUMN pending jsonb, ADD COLUMN lease_token varchar(36), ADD COLUMN lease_until timestamptz,
        ADD COLUMN next_sync_at timestamptz, ADD COLUMN restart_requested boolean NOT NULL DEFAULT false, ADD COLUMN sync_error boolean NOT NULL DEFAULT false""")
    op.execute("""CREATE POLICY billing_terms_read ON company_subscriptions FOR SELECT USING (
        organization_id::text=current_setting('app.organization_id',true)
        AND coalesce(current_setting('app.shipper_id',true),'')=''
        AND coalesce(current_setting('app.driver_id',true),'')='')""")
    op.execute('CREATE INDEX company_billing_due ON company_billing (next_sync_at) WHERE consented_at IS NOT NULL')


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM company_billing WHERE consented_at IS NOT NULL) THEN RAISE EXCEPTION 'Preserve recurring subscription records before rollback'; END IF; END $$")
    op.execute('DROP POLICY billing_terms_read ON company_subscriptions')
    op.execute('DROP INDEX company_billing_due')
    for column in ('consented_at','consent_terms','setup_terms_version','stripe_subscription','subscription_status','period_end','paid_until','invoice_url','pending','lease_token','lease_until','next_sync_at','restart_requested','sync_error'):
        op.execute('ALTER TABLE company_billing DROP COLUMN ' + column)
    op.execute('ALTER TABLE organizations DROP COLUMN billing_enrolled, DROP COLUMN billing_access_until')
