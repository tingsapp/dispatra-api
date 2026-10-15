"""Company SaaS cards and post-trial access. No delivery payments or charges."""
from alembic import op
revision = '0050_company_payment_methods'
down_revision = '0049_chat_agent'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE organizations ADD COLUMN payment_method_valid_until timestamptz')
    op.execute('''CREATE TABLE company_billing (
        organization_id uuid PRIMARY KEY REFERENCES organizations(id), stripe_customer varchar(255) UNIQUE,
        payment_method varchar(255), brand varchar(40), last4 varchar(4), exp_month integer, exp_year integer,
        setup_session varchar(255), completed_session varchar(255), setup_nonce varchar(64), updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK (exp_month BETWEEN 1 AND 12), CHECK (last4 ~ '^[0-9]{4}$'))''')
    op.execute('ALTER TABLE company_billing ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE company_billing FORCE ROW LEVEL SECURITY')
    owner = "current_setting('app.platform',true)='true' AND coalesce(current_setting('app.organization_id',true),'')=''"
    tenant = "organization_id::text=current_setting('app.organization_id',true) AND coalesce(current_setting('app.shipper_id',true),'')='' AND coalesce(current_setting('app.driver_id',true),'')=''"
    op.execute(f'CREATE POLICY company_billing_scope ON company_billing USING (({owner}) OR ({tenant})) WITH CHECK (({owner}) OR ({tenant}))')
    op.execute('GRANT SELECT,INSERT,UPDATE ON company_billing TO dispatra_app')


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM company_billing WHERE stripe_customer IS NOT NULL) THEN RAISE EXCEPTION 'Preserve company payment records before rollback'; END IF; END $$")
    op.execute('DROP TABLE company_billing')
    op.execute('ALTER TABLE organizations DROP COLUMN payment_method_valid_until')
