"""Authenticated shipper completion of explicitly requested email drafts."""
from alembic import op

revision = '0066_email_draft_completion'
down_revision = '0065_public_support_tickets'
branch_labels = depends_on = None
COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""
INVITED = """organization_id::text = nullif(current_setting('app.organization_id', true), '')
AND shipper_id::text = nullif(current_setting('app.shipper_id', true), '')
AND coalesce(current_setting('app.driver_id', true), '') = ''
AND completion_requested_at IS NOT NULL AND status IN ('NEEDS_REVIEW','UNKNOWN_SENDER')"""


def upgrade():
    op.execute('ALTER TABLE email_intakes ADD COLUMN completion_requested_at timestamptz, ADD COLUMN completion_submitted_at timestamptz')
    op.execute('DROP POLICY tenant_isolation ON email_intakes')
    op.execute(f'CREATE POLICY tenant_isolation ON email_intakes USING (({COMPANY}) OR ({INVITED})) WITH CHECK (({COMPANY}) OR ({INVITED}))')
    op.execute('ALTER TABLE email_deliveries ADD COLUMN intake_id uuid')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_intake FOREIGN KEY (organization_id, intake_id) REFERENCES email_intakes(organization_id,id)')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK (num_nonnulls(quote_id, notification_id, intake_id) = 1)')


def downgrade():
    # Preserve sent/requested mail history; an old binary cannot represent this new source.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM email_deliveries WHERE intake_id IS NOT NULL) THEN RAISE EXCEPTION 'Email draft delivery history must be archived before downgrade'; END IF; END $$")
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK (num_nonnulls(quote_id, notification_id) = 1)')
    op.execute('ALTER TABLE email_deliveries DROP COLUMN intake_id')
    op.execute('DROP POLICY tenant_isolation ON email_intakes')
    op.execute(f'CREATE POLICY tenant_isolation ON email_intakes USING ({COMPANY}) WITH CHECK ({COMPANY})')
    op.execute('ALTER TABLE email_intakes DROP COLUMN completion_requested_at, DROP COLUMN completion_submitted_at')
