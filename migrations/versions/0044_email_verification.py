"""Gate new trials and protect one-time email verification challenges with forced RLS."""
from alembic import op
revision = '0044_email_verification'
down_revision = '0043_company_activity_indexes'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('ALTER TABLE organizations ADD COLUMN verification_pending boolean NOT NULL DEFAULT false')
    op.execute('ALTER TABLE users ADD CONSTRAINT verification_user_scope UNIQUE (organization_id,id)')
    op.execute('''CREATE TABLE email_verifications (
        id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id),
        user_id uuid NOT NULL, email varchar(254) NOT NULL,
        token_hash text NOT NULL UNIQUE, code_hash text NOT NULL, secret text,
        expires_at timestamptz NOT NULL, resend_at timestamptz NOT NULL,
        attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0), consumed_at timestamptz,
        delivery_status varchar(16) NOT NULL DEFAULT 'PENDING'
        CHECK (delivery_status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN')),
        FOREIGN KEY (organization_id,user_id) REFERENCES users(organization_id,id)
    )''')
    op.execute('CREATE INDEX email_verification_user ON email_verifications(organization_id,user_id)')
    op.execute('ALTER TABLE email_verifications ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE email_verifications FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY verification_tenant ON email_verifications USING (
        organization_id::text = nullif(current_setting('app.organization_id',true),'')
        OR current_setting('app.platform',true) = 'true') WITH CHECK (
        organization_id::text = nullif(current_setting('app.organization_id',true),'')
        OR current_setting('app.platform',true) = 'true')""")
    op.execute("""CREATE POLICY verification_lookup ON email_verifications FOR SELECT USING (
        token_hash = nullif(current_setting('app.verification_token',true),''))""")
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON email_verifications TO dispatra_app')

def downgrade():
    op.execute('DROP TABLE email_verifications')
    op.execute('ALTER TABLE users DROP CONSTRAINT verification_user_scope')
    op.execute('ALTER TABLE organizations DROP COLUMN verification_pending')
