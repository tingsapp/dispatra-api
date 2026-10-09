"""Retire V1 invoicing while preserving private historical records."""
from alembic import op
from sqlalchemy import text

revision = '0028_remove_invoicing'
down_revision = '0027_vehicle_unit_numbers'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SELECT set_config('app.platform', 'true', true)")
    op.execute('LOCK TABLE orders, invoices, email_deliveries, notifications IN ACCESS EXCLUSIVE MODE')
    op.execute("""CREATE TABLE archived_invoice_notifications AS SELECT * FROM notifications
        WHERE kind LIKE 'invoice.%' OR kind = 'order.invoiced'""")
    op.execute("""CREATE TABLE archived_invoice_email_deliveries AS SELECT * FROM email_deliveries
        WHERE invoice_id IS NOT NULL OR notification_id IN (SELECT id FROM archived_invoice_notifications)""")
    # Retire pending sends without erasing their original delivery statuses/history.
    op.execute("""UPDATE outbox_events SET published_at = coalesce(published_at, now())
        WHERE event_type = 'email.requested' AND entity_id IN (SELECT id FROM archived_invoice_email_deliveries)""")
    op.execute('DELETE FROM email_deliveries WHERE id IN (SELECT id FROM archived_invoice_email_deliveries)')
    op.execute('DELETE FROM notifications WHERE id IN (SELECT id FROM archived_invoice_notifications)')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries DROP COLUMN invoice_id')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK (num_nonnulls(quote_id, notification_id) = 1)')
    op.execute('ALTER TABLE invoices RENAME TO archived_invoices')
    op.execute("UPDATE orders SET status = 'COMPLETED', version = version + 1, updated_at = now() WHERE status = 'INVOICED'")
    # The frozen initial migration used an unnamed status constraint; discover it by its expression.
    db = op.get_bind()
    for name in db.scalars(text("""SELECT conname FROM pg_constraint WHERE conrelid = 'orders'::regclass
        AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%INVOICED%'""")):
        op.drop_constraint(name, 'orders', type_='check')
    op.execute("ALTER TABLE orders ADD CONSTRAINT orders_status_check CHECK (status IN ('NEW','ASSIGNED','IN_PROGRESS','COMPLETED','CANCELLED'))")
    owner = db.dialect.identifier_preparer.quote(db.scalar(text('SELECT current_user')))
    for table in ['archived_invoices', 'archived_invoice_email_deliveries', 'archived_invoice_notifications']:
        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC, dispatra_app')
        for policy in db.scalars(text('SELECT policyname FROM pg_policies WHERE schemaname = :schema AND tablename = :table'),
                                  {'schema':'public', 'table':table}):
            op.execute(f'DROP POLICY {db.dialect.identifier_preparer.quote(policy)} ON {table}')
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY archive_owner ON {table} TO {owner} USING (true) WITH CHECK (true)')


def downgrade():
    op.execute("SELECT set_config('app.platform', 'true', true)")
    op.execute('ALTER TABLE orders DROP CONSTRAINT orders_status_check')
    op.execute("ALTER TABLE orders ADD CONSTRAINT orders_status_check CHECK (status IN ('NEW','ASSIGNED','IN_PROGRESS','COMPLETED','INVOICED','CANCELLED'))")
    op.execute('ALTER TABLE archived_invoices RENAME TO invoices')
    op.execute('DROP POLICY archive_owner ON invoices')
    op.execute("""CREATE POLICY tenant_isolation ON invoices USING (
        (organization_id::text = nullif(current_setting('app.organization_id', true), '')
         OR current_setting('app.platform', true) = 'true')
        AND coalesce(current_setting('app.driver_id', true), '') = ''
        AND order_id IN (SELECT id FROM orders))""")
    op.execute('GRANT SELECT, INSERT ON invoices TO dispatra_app')
    op.execute('ALTER TABLE email_deliveries DROP CONSTRAINT email_delivery_source')
    op.execute('ALTER TABLE email_deliveries ADD COLUMN invoice_id uuid')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_invoice_fk FOREIGN KEY (organization_id, invoice_id) REFERENCES invoices(organization_id, id)')
    op.execute('ALTER TABLE email_deliveries ADD CONSTRAINT email_delivery_source CHECK (num_nonnulls(quote_id, invoice_id, notification_id) = 1)')
    op.execute("CREATE UNIQUE INDEX uq_email_pending_invoice ON email_deliveries (organization_id, invoice_id, recipient) WHERE invoice_id IS NOT NULL AND status IN ('PENDING','SENDING')")
    op.execute('INSERT INTO notifications SELECT * FROM archived_invoice_notifications')
    # INSERT by column name because the restored invoice_id is appended to the table.
    db = op.get_bind()
    columns = ', '.join(db.dialect.identifier_preparer.quote(column) for column in db.scalars(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'archived_invoice_email_deliveries' ORDER BY ordinal_position")))
    op.execute(f'INSERT INTO email_deliveries ({columns}) SELECT {columns} FROM archived_invoice_email_deliveries')
    op.execute("UPDATE orders SET status = 'INVOICED' WHERE status = 'COMPLETED' AND id IN (SELECT order_id FROM invoices)")
    # Former invoice requests remain acknowledged: rollback must not send old mail automatically.
    op.execute('DROP TABLE archived_invoice_email_deliveries, archived_invoice_notifications')
