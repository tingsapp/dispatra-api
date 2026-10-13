"""Resolve company from role/login and reserve contact identifiers within each role."""
from alembic import op

revision = '0038_role_login'
down_revision = '0037_agent_monitor'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN phone varchar(50) NOT NULL DEFAULT ''")
    # Formatting and the optional Canadian country prefix do not create a new phone.
    op.execute("""CREATE FUNCTION account_phone(value text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
        SELECT nullif(CASE WHEN length(digits) = 11 AND left(digits, 1) = '1'
            THEN substr(digits, 2) ELSE digits END, '')
        FROM (SELECT regexp_replace(value, '[^0-9]', '', 'g') AS digits) normalized $$""")
    op.execute("UPDATE users u SET phone = s.phone FROM shippers s WHERE u.shipper_id = s.id")
    op.execute("UPDATE users u SET phone = d.phone FROM drivers d WHERE u.driver_id = d.id")
    op.execute("""UPDATE users u SET phone = coalesce(s.data->>'phone', '') FROM company_settings s
        WHERE u.organization_id = s.organization_id AND u.role = 'DISPATCHER' AND u.active""")
    # Do not rewrite identities or discard accounts to resolve ambiguous old logins.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM users GROUP BY role, lower(login_id) HAVING count(*) > 1)
        OR EXISTS (SELECT 1 FROM users WHERE account_phone(phone) IS NOT NULL
                   GROUP BY role, account_phone(phone) HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Resolve duplicate email/phone accounts within each role before applying 0038_role_login';
        END IF;
    END $$""")
    op.execute('ALTER TABLE users DROP CONSTRAINT users_scope_login_id_key')
    op.execute('UPDATE users SET login_id = lower(login_id)')
    op.execute('CREATE UNIQUE INDEX users_role_login_unique ON users (role, lower(login_id))')
    op.execute('CREATE UNIQUE INDEX users_role_phone_unique ON users (role, account_phone(phone)) WHERE account_phone(phone) IS NOT NULL')
    op.execute("""CREATE POLICY login_lookup ON users FOR SELECT USING (
        role = nullif(current_setting('app.login_role', true), '') AND
        login_id = nullif(current_setting('app.login_id', true), ''))""")
    # Keep the account contact reserve aligned for every writer, including imports,
    # dispatcher edits and both native/web self-service profile endpoints.
    op.execute("""CREATE FUNCTION account_contact_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        NEW.login_id := lower(NEW.login_id);
        IF NEW.shipper_id IS NOT NULL THEN
            SELECT phone INTO NEW.phone FROM shippers WHERE id = NEW.shipper_id;
        ELSIF NEW.driver_id IS NOT NULL THEN
            SELECT phone INTO NEW.phone FROM drivers WHERE id = NEW.driver_id;
        ELSIF NEW.role = 'DISPATCHER' AND NEW.active AND (TG_OP = 'UPDATE' OR NEW.phone = '') THEN
            NEW.phone := coalesce((SELECT data->>'phone' FROM company_settings WHERE organization_id = NEW.organization_id), NEW.phone, '');
        END IF;
        NEW.phone := coalesce(NEW.phone, '');
        RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER account_contact_insert BEFORE INSERT OR UPDATE OF login_id, active ON users FOR EACH ROW EXECUTE FUNCTION account_contact_insert()')
    op.execute("""CREATE FUNCTION account_contact_sync() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME = 'shippers' THEN
            UPDATE users SET phone = NEW.phone WHERE shipper_id = NEW.id;
        ELSIF TG_TABLE_NAME = 'drivers' THEN
            UPDATE users SET phone = NEW.phone WHERE driver_id = NEW.id;
        ELSE
            UPDATE users SET phone = coalesce(NEW.data->>'phone', '')
                WHERE organization_id = NEW.organization_id AND role = 'DISPATCHER' AND active;
        END IF;
        RETURN NEW;
    END $$""")
    for table in ('shippers', 'drivers'):
        op.execute(f'CREATE TRIGGER account_contact_sync AFTER UPDATE OF phone ON {table} FOR EACH ROW EXECUTE FUNCTION account_contact_sync()')
    op.execute('CREATE TRIGGER account_contact_sync AFTER INSERT OR UPDATE OF data ON company_settings FOR EACH ROW EXECUTE FUNCTION account_contact_sync()')


def downgrade():
    # Old company-wide login uniqueness may need manual reconciliation after
    # accounts with the same email in different roles have been created.
    for table in ('shippers', 'drivers', 'company_settings'):
        op.execute(f'DROP TRIGGER account_contact_sync ON {table}')
    op.execute('DROP TRIGGER account_contact_insert ON users')
    op.execute('DROP FUNCTION account_contact_sync()')
    op.execute('DROP FUNCTION account_contact_insert()')
    op.execute('DROP POLICY login_lookup ON users')
    op.execute('DROP INDEX users_role_phone_unique')
    op.execute('DROP INDEX users_role_login_unique')
    op.execute('ALTER TABLE users ADD CONSTRAINT users_scope_login_id_key UNIQUE (scope, login_id)')
    op.execute('ALTER TABLE users DROP COLUMN phone')
    op.execute('DROP FUNCTION account_phone(text)')
