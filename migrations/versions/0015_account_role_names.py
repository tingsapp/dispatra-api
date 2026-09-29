"""Rename Dispatra account roles and the legacy shipper identity table.

Existing migrations remain frozen.  The operational shippers table is unchanged;
the former customers table holds its login/profile identity records.
"""
from alembic import op

revision = '0015_account_role_names'
down_revision = '0014_platform_administration'
branch_labels = None
depends_on = None


NEW_CHECK = """(role = 'ADMIN' AND organization_id IS NULL AND shipper_id IS NULL AND driver_id IS NULL AND scope = 'platform')
 OR (role = 'DISPATCHER' AND organization_id IS NOT NULL AND shipper_id IS NULL AND driver_id IS NULL AND scope = organization_id::text)
 OR (role = 'SHIPPER' AND organization_id IS NOT NULL AND shipper_id IS NOT NULL AND driver_id IS NULL AND scope = organization_id::text)
 OR (role = 'DRIVER' AND organization_id IS NOT NULL AND driver_id IS NOT NULL AND shipper_id IS NULL AND scope = organization_id::text)"""

OLD_CHECK = """(role = 'PLATFORM_OWNER' AND organization_id IS NULL AND customer_id IS NULL AND driver_id IS NULL AND scope = 'platform')
 OR (role = 'DISPATCHER' AND organization_id IS NOT NULL AND customer_id IS NULL AND driver_id IS NULL AND scope = organization_id::text)
 OR (role = 'CUSTOMER' AND organization_id IS NOT NULL AND customer_id IS NOT NULL AND driver_id IS NULL AND scope = organization_id::text)
 OR (role = 'DRIVER' AND organization_id IS NOT NULL AND driver_id IS NOT NULL AND customer_id IS NULL AND scope = organization_id::text)"""


def _rename_rls_setting(before: str, after: str):
    # Keep every existing policy's tenant and role restrictions.  Only the
    # transaction-local shipper context key changes; Stripe's own customer ID
    # and table retain their provider-defined names.
    op.execute(f"""DO $migration$
    DECLARE policy record;
    BEGIN
      FOR policy IN
        SELECT tablename, policyname, qual, with_check
        FROM pg_policies
        WHERE schemaname = 'public'
          AND (qual LIKE '%{before}%' OR with_check LIKE '%{before}%')
      LOOP
        EXECUTE format('ALTER POLICY %I ON %I USING (%s) WITH CHECK (%s)',
          policy.policyname, policy.tablename,
          replace(policy.qual, '{before}', '{after}'),
          replace(policy.with_check, '{before}', '{after}'));
      END LOOP;
    END $migration$""")


def upgrade():
    op.execute('ALTER TABLE users DROP CONSTRAINT user_role_scope')
    op.execute('DROP INDEX users_single_platform_owner')
    op.execute('ALTER TABLE customers RENAME TO shipper_accounts')
    op.execute('ALTER INDEX ix_customers_organization_id RENAME TO ix_shipper_accounts_organization_id')
    op.execute('ALTER TABLE users RENAME COLUMN customer_id TO shipper_id')
    op.execute('ALTER TABLE users RENAME CONSTRAINT users_customer_id_key TO users_shipper_id_key')
    op.execute("UPDATE users SET role = CASE role WHEN 'PLATFORM_OWNER' THEN 'ADMIN' WHEN 'CUSTOMER' THEN 'SHIPPER' ELSE role END")
    op.execute(f'ALTER TABLE users ADD CONSTRAINT user_role_scope CHECK ({NEW_CHECK})')
    op.execute("CREATE UNIQUE INDEX users_single_admin ON users ((true)) WHERE role = 'ADMIN'")
    _rename_rls_setting('app.customer_id', 'app.shipper_id')


def downgrade():
    _rename_rls_setting('app.shipper_id', 'app.customer_id')
    op.execute('ALTER TABLE users DROP CONSTRAINT user_role_scope')
    op.execute('DROP INDEX users_single_admin')
    op.execute("UPDATE users SET role = CASE role WHEN 'ADMIN' THEN 'PLATFORM_OWNER' WHEN 'SHIPPER' THEN 'CUSTOMER' ELSE role END")
    op.execute('ALTER TABLE users RENAME CONSTRAINT users_shipper_id_key TO users_customer_id_key')
    op.execute('ALTER TABLE users RENAME COLUMN shipper_id TO customer_id')
    op.execute('ALTER INDEX ix_shipper_accounts_organization_id RENAME TO ix_customers_organization_id')
    op.execute('ALTER TABLE shipper_accounts RENAME TO customers')
    op.execute(f'ALTER TABLE users ADD CONSTRAINT user_role_scope CHECK ({OLD_CHECK})')
    op.execute("CREATE UNIQUE INDEX users_single_platform_owner ON users ((true)) WHERE role = 'PLATFORM_OWNER'")
