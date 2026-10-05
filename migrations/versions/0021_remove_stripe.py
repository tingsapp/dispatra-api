"""V1 has no card payments: drop the Stripe Connect connection and Shipper customer references."""
from alembic import op

revision = '0021_remove_stripe'
down_revision = '0020_event_pipeline'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('DROP TABLE stripe_customers')
    op.execute('DROP TABLE stripe_connections')


def downgrade():
    common = '''id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id),
      version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP'''
    op.execute(f'''CREATE TABLE stripe_connections ({common}, account_id varchar(100) NOT NULL, livemode boolean NOT NULL,
      UNIQUE (account_id,livemode), UNIQUE (organization_id,livemode), UNIQUE (organization_id,id))''')
    op.execute(f'''CREATE TABLE stripe_customers ({common}, shipper_id uuid NOT NULL,
      account_id varchar(100) NOT NULL, livemode boolean NOT NULL, customer_id varchar(100),
      UNIQUE (organization_id,shipper_id,account_id,livemode), UNIQUE (account_id,customer_id,livemode),
      FOREIGN KEY (organization_id,shipper_id) REFERENCES shippers(organization_id,id))''')
    for table in ['stripe_connections', 'stripe_customers']:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        shipper = "AND (coalesce(current_setting('app.shipper_id',true),'') = '' OR shipper_id::text = current_setting('app.shipper_id',true))" if table == 'stripe_customers' else ''
        predicate = f"""(organization_id::text = nullif(current_setting('app.organization_id',true),'')
          OR current_setting('app.platform',true) = 'true')
          AND coalesce(current_setting('app.driver_id',true),'') = '' {shipper}"""
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})')
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO dispatra_app')
