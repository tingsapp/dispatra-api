"""Company accounts, customer access, sessions and isolation."""
from alembic import op
SCHEMA_SQL = ['\nCREATE TABLE login_buckets (\n\tkey VARCHAR(64) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tresets_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (key)\n)\n\n', '\nCREATE TABLE organizations (\n\tid UUID NOT NULL, \n\tslug VARCHAR(63) NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (slug)\n)\n\n', "\nCREATE TABLE customers (\n\tid UUID NOT NULL, \n\torganization_id UUID NOT NULL, \n\tnumber VARCHAR(50) NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tcontact_name VARCHAR(160) NOT NULL, \n\temail VARCHAR(254) NOT NULL, \n\tphone VARCHAR(50) NOT NULL, \n\taddress VARCHAR(500) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (organization_id, number), \n\tUNIQUE (organization_id, id), \n\tCHECK (status IN ('ACTIVE','INACTIVE','ON_HOLD')), \n\tFOREIGN KEY(organization_id) REFERENCES organizations (id)\n)\n\n", 'CREATE INDEX ix_customers_organization_id ON customers (organization_id)', '\nCREATE TABLE operations (\n\tkey VARCHAR(160) NOT NULL, \n\torganization_id UUID, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\tresult JSONB NOT NULL, \n\tPRIMARY KEY (key), \n\tFOREIGN KEY(organization_id) REFERENCES organizations (id)\n)\n\n', "\nCREATE TABLE users (\n\tid UUID NOT NULL, \n\torganization_id UUID, \n\tcustomer_id UUID, \n\tscope VARCHAR(40) NOT NULL, \n\tlogin_id VARCHAR(100) NOT NULL, \n\tpassword_hash TEXT NOT NULL, \n\trole VARCHAR(30) NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (scope, login_id), \n\tUNIQUE (customer_id), \n\tFOREIGN KEY(organization_id, customer_id) REFERENCES customers (organization_id, id), \n\tCHECK ((role = 'PLATFORM_OWNER' AND organization_id IS NULL AND customer_id IS NULL AND scope = 'platform') OR (role = 'DISPATCHER' AND organization_id IS NOT NULL AND customer_id IS NULL AND scope = organization_id::text) OR (role = 'CUSTOMER' AND organization_id IS NOT NULL AND customer_id IS NOT NULL AND scope = organization_id::text)), \n\tFOREIGN KEY(organization_id) REFERENCES organizations (id)\n)\n\n", 'CREATE INDEX ix_users_organization_id ON users (organization_id)', '\nCREATE TABLE audit_events (\n\tid UUID NOT NULL, \n\torganization_id UUID, \n\tactor_id UUID NOT NULL, \n\taction VARCHAR(80) NOT NULL, \n\tentity_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(organization_id) REFERENCES organizations (id), \n\tFOREIGN KEY(actor_id) REFERENCES users (id)\n)\n\n', 'CREATE INDEX ix_audit_events_organization_id ON audit_events (organization_id)', '\nCREATE TABLE login_sessions (\n\ttoken_hash VARCHAR(64) NOT NULL, \n\tuser_id UUID NOT NULL, \n\torganization_id UUID, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (token_hash), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(organization_id) REFERENCES organizations (id)\n)\n\n', 'CREATE INDEX ix_login_sessions_expires_at ON login_sessions (expires_at)', 'CREATE INDEX ix_login_sessions_user_id ON login_sessions (user_id)']

revision = '0001_accounts'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    for statement in SCHEMA_SQL:
        op.execute(statement)
    op.execute("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dispatra_app') THEN
        CREATE ROLE dispatra_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
      END IF;
    END $$""")
    op.execute('GRANT USAGE ON SCHEMA public TO dispatra_app')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dispatra_app')
    op.execute('REVOKE ALL ON alembic_version FROM dispatra_app')
    op.execute('REVOKE UPDATE, DELETE ON audit_events, operations FROM dispatra_app')
    for table in ['customers','users','audit_events','operations']:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        expression = "organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true'"
        if table == 'customers':
            expression = f"({expression}) AND (coalesce(current_setting('app.customer_id', true), '') = '' OR id::text = current_setting('app.customer_id', true))"
        if table == 'users':
            expression += " OR (organization_id IS NULL AND coalesce(current_setting('app.organization_id', true), '') = '')"
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({expression}) WITH CHECK ({expression})')

def downgrade():
    for table in ['login_sessions', 'audit_events', 'users', 'operations', 'customers', 'organizations', 'login_buckets']:
        op.drop_table(table)

