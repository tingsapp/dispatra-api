"""Dispatch agent: `dispatch_decisions` keeps every evaluation (candidates, exclusions, ranking, mode, outcome).

Company-dispatcher scope only: Shipper and driver sessions never read it. Audit rows may now have no user actor
when the agent assigns in AUTO mode; the matching event is recorded with actor type `AGENT`.
"""
from alembic import op

revision = '0025_dispatch_agent'
down_revision = '0024_mailbox_sending'
branch_labels = None
depends_on = None

COMPANY = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"""


def upgrade():
    op.execute("""CREATE TABLE dispatch_decisions (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        order_id uuid NOT NULL,
        order_version integer NOT NULL,
        mode varchar(10) NOT NULL,
        status varchar(20) NOT NULL,
        ranked_by varchar(10) NOT NULL,
        candidates jsonb NOT NULL,
        excluded jsonb NOT NULL,
        summary text NOT NULL,
        driver_id uuid,
        decided_by uuid REFERENCES users(id),
        error_code varchar(80),
        version integer NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT dispatch_decision_mode CHECK (mode IN ('AUTO','MANUAL')),
        CONSTRAINT dispatch_decision_status CHECK (status IN ('SUGGESTED','ASSIGNED','NO_CANDIDATE')),
        CONSTRAINT dispatch_decision_ranked_by CHECK (ranked_by IN ('AI','RULES')),
        CONSTRAINT dispatch_decisions_organization_id_id_key UNIQUE (organization_id, id),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id),
        FOREIGN KEY (organization_id, driver_id) REFERENCES drivers(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_dispatch_decisions_order ON dispatch_decisions (organization_id, order_id, created_at DESC)')
    op.execute('ALTER TABLE dispatch_decisions ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE dispatch_decisions FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON dispatch_decisions USING ({COMPANY}) WITH CHECK ({COMPANY})')
    op.execute('GRANT SELECT, INSERT, UPDATE ON dispatch_decisions TO dispatra_app')
    op.execute('ALTER TABLE audit_events ALTER COLUMN actor_id DROP NOT NULL')


def downgrade():
    op.execute('DROP TABLE dispatch_decisions')
    op.execute('DELETE FROM audit_events WHERE actor_id IS NULL')
    op.execute('ALTER TABLE audit_events ALTER COLUMN actor_id SET NOT NULL')
