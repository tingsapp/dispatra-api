"""Merge the Shipper login/profile identity (`shipper_accounts`, formerly `customers`) into `shippers`.

Both tables already shared IDs. Identity columns move onto `shippers`; the account's user row now
references `shippers`. Account-only records created through the legacy customer-account API have no
operational profile and keep a NULL warehouse, so operational Shipper queries continue to exclude them.
Migrations 0001-0015 remain frozen.
"""
from alembic import op

revision = '0016_merge_shipper_identity'
down_revision = '0015_account_role_names'
branch_labels = None
depends_on = None

STATUS = "status IN ('ACTIVE','INACTIVE','ON_HOLD')"
IDENTITY = 'number, name, contact_name, email, phone, address, status'


def upgrade():
    op.execute("""ALTER TABLE shippers
        ADD COLUMN number varchar(50), ADD COLUMN name varchar(160),
        ADD COLUMN contact_name varchar(160) NOT NULL DEFAULT '', ADD COLUMN email varchar(254) NOT NULL DEFAULT '',
        ADD COLUMN phone varchar(50) NOT NULL DEFAULT '', ADD COLUMN address varchar(500) NOT NULL DEFAULT '',
        ADD COLUMN status varchar(20) NOT NULL DEFAULT 'ACTIVE'""")
    op.execute('ALTER TABLE shippers ALTER COLUMN warehouse DROP NOT NULL')
    # Legacy account-only identities become shippers without an operational profile.
    op.execute(f"""INSERT INTO shippers (id, organization_id, version, created_at, updated_at, kind, company_name, warehouse,
            rate_card_id, terms, discount, instructions, archived_at, {IDENTITY})
        SELECT a.id, a.organization_id, a.version, a.created_at, a.updated_at, 'BUSINESS', a.name, NULL,
            NULL, 'NET30', '{{"kind": "NONE", "value": "0"}}'::jsonb, '', NULL, a.number, a.name, a.contact_name, a.email, a.phone, a.address, a.status
        FROM shipper_accounts a WHERE NOT EXISTS (SELECT 1 FROM shippers s WHERE s.id = a.id)""")
    op.execute("""UPDATE shippers s SET number = a.number, name = a.name, contact_name = a.contact_name, email = a.email,
            phone = a.phone, address = a.address, status = a.status, version = GREATEST(s.version, a.version)
        FROM shipper_accounts a WHERE a.id = s.id""")
    op.execute('ALTER TABLE shippers ALTER COLUMN number SET NOT NULL, ALTER COLUMN name SET NOT NULL')
    op.execute(f'ALTER TABLE shippers ADD CONSTRAINT shippers_status_check CHECK ({STATUS})')
    op.execute('ALTER TABLE shippers ADD CONSTRAINT shippers_organization_id_number_key UNIQUE (organization_id, number)')
    op.execute('ALTER TABLE users DROP CONSTRAINT users_organization_id_customer_id_fkey')
    op.execute('ALTER TABLE shippers DROP CONSTRAINT shippers_organization_id_id_fkey')
    op.execute('ALTER TABLE users ADD CONSTRAINT users_organization_id_shipper_id_fkey FOREIGN KEY (organization_id, shipper_id) REFERENCES shippers(organization_id, id)')
    op.execute('DROP TABLE shipper_accounts')


def downgrade():
    op.execute(f"""CREATE TABLE shipper_accounts (
        id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id),
        number varchar(50) NOT NULL, name varchar(160) NOT NULL, contact_name varchar(160) NOT NULL, email varchar(254) NOT NULL,
        phone varchar(50) NOT NULL, address varchar(500) NOT NULL, status varchar(20) NOT NULL, version integer NOT NULL,
        created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
        UNIQUE (organization_id, number), UNIQUE (organization_id, id), CHECK ({STATUS}))""")
    op.execute('CREATE INDEX ix_shipper_accounts_organization_id ON shipper_accounts (organization_id)')
    op.execute(f"""INSERT INTO shipper_accounts (id, organization_id, {IDENTITY}, version, created_at, updated_at)
        SELECT id, organization_id, {IDENTITY}, version, created_at, updated_at FROM shippers""")
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON shipper_accounts TO dispatra_app')
    op.execute('ALTER TABLE shipper_accounts ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE shipper_accounts FORCE ROW LEVEL SECURITY')
    predicate = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
        AND (coalesce(current_setting('app.shipper_id', true), '') = '' OR id::text = current_setting('app.shipper_id', true))"""
    op.execute(f'CREATE POLICY tenant_isolation ON shipper_accounts USING ({predicate}) WITH CHECK ({predicate})')
    op.execute('ALTER TABLE users DROP CONSTRAINT users_organization_id_shipper_id_fkey')
    op.execute('ALTER TABLE users ADD CONSTRAINT users_organization_id_customer_id_fkey FOREIGN KEY (organization_id, shipper_id) REFERENCES shipper_accounts(organization_id, id)')
    op.execute('DELETE FROM shippers WHERE warehouse IS NULL')
    op.execute('ALTER TABLE shippers ADD CONSTRAINT shippers_organization_id_id_fkey FOREIGN KEY (organization_id, id) REFERENCES shipper_accounts(organization_id, id)')
    op.execute('ALTER TABLE shippers ALTER COLUMN warehouse SET NOT NULL')
    op.execute('ALTER TABLE shippers DROP CONSTRAINT shippers_organization_id_number_key, DROP CONSTRAINT shippers_status_check')
    op.execute('ALTER TABLE shippers DROP COLUMN number, DROP COLUMN name, DROP COLUMN contact_name, DROP COLUMN email, DROP COLUMN phone, DROP COLUMN address, DROP COLUMN status')
