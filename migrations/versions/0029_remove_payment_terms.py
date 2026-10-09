"""Retire shipper payment defaults without deleting historical terms."""
from alembic import op
from sqlalchemy import text

revision = '0029_remove_payment_terms'
down_revision = '0028_remove_invoicing'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SELECT set_config('app.platform', 'true', true)")
    op.execute('LOCK TABLE shippers IN ACCESS EXCLUSIVE MODE')
    op.execute('CREATE TABLE archived_shipper_payment_terms AS SELECT id, organization_id, terms FROM shippers')
    op.execute('ALTER TABLE shippers DROP COLUMN terms')
    op.execute('REVOKE ALL ON TABLE archived_shipper_payment_terms FROM PUBLIC, dispatra_app')
    op.execute('ALTER TABLE archived_shipper_payment_terms ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE archived_shipper_payment_terms FORCE ROW LEVEL SECURITY')
    db = op.get_bind()
    owner = db.dialect.identifier_preparer.quote(db.scalar(text('SELECT current_user')))
    op.execute(f'CREATE POLICY archive_owner ON archived_shipper_payment_terms TO {owner} USING (true) WITH CHECK (true)')


def downgrade():
    op.execute("SELECT set_config('app.platform', 'true', true)")
    op.execute('ALTER TABLE shippers ADD COLUMN terms varchar(20) NOT NULL DEFAULT \'NET30\'')
    op.execute('UPDATE shippers SET terms = history.terms FROM archived_shipper_payment_terms history WHERE shippers.id = history.id')
    op.execute('DROP TABLE archived_shipper_payment_terms')
