"""The company mailbox also sends: SMTP server/port beside IMAP, notification emails to Shippers, and a per-Shipper opt-out."""
from alembic import op

revision = '0024_mailbox_sending'
down_revision = '0023_company_default_service'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE mailbox_connections ADD COLUMN smtp_host varchar(253), ADD COLUMN smtp_port integer')
    op.execute("UPDATE mailbox_connections SET smtp_host = regexp_replace(host, '^imap\\.', 'smtp.'), smtp_port = 465")
    op.execute('ALTER TABLE mailbox_connections ALTER COLUMN smtp_host SET NOT NULL, ALTER COLUMN smtp_port SET NOT NULL')
    op.execute('ALTER TABLE mailbox_connections ADD CONSTRAINT mailbox_smtp_port CHECK (smtp_port BETWEEN 1 AND 65535)')
    op.execute('ALTER TABLE email_deliveries ADD COLUMN notification_id uuid')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK (num_nonnulls(quote_id, invoice_id, notification_id) = 1)')
    op.execute('''ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_notification_fk
        FOREIGN KEY (organization_id, notification_id) REFERENCES notifications(organization_id, id)''')
    op.execute('CREATE UNIQUE INDEX uq_email_notification ON email_deliveries (organization_id, notification_id) WHERE notification_id IS NOT NULL')
    op.execute('ALTER TABLE shippers ADD COLUMN email_updates boolean NOT NULL DEFAULT true')


def downgrade():
    op.execute('ALTER TABLE shippers DROP COLUMN email_updates')
    op.execute('DELETE FROM email_deliveries WHERE notification_id IS NOT NULL')
    op.execute('DROP INDEX uq_email_notification')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_notification_fk')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries DROP COLUMN notification_id')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK ((quote_id IS NULL) <> (invoice_id IS NULL))')
    op.execute('ALTER TABLE mailbox_connections DROP CONSTRAINT mailbox_smtp_port')
    op.execute('ALTER TABLE mailbox_connections DROP COLUMN smtp_host, DROP COLUMN smtp_port')
