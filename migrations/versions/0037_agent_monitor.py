"""Owner-only run logs, platform diagnostics and approved technical repairs."""
from alembic import op

revision = '0037_agent_monitor'
down_revision = '0036_single_dispatcher'
branch_labels = None
depends_on = None
OWNER = "current_setting('app.platform', true) = 'true' AND coalesce(current_setting('app.shipper_id', true), '') = '' AND coalesce(current_setting('app.driver_id', true), '') = ''"


def upgrade():
    op.execute("""CREATE TABLE agent_runs (
        id uuid PRIMARY KEY, organization_id uuid REFERENCES organizations(id),
        agent varchar(16) NOT NULL CHECK (agent IN ('ORDER','DISPATCH','TECHNICAL')),
        status varchar(16) NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
        step varchar(300) NOT NULL, progress integer NOT NULL CHECK (progress BETWEEN 0 AND 100),
        created_at timestamptz NOT NULL, heartbeat_at timestamptz NOT NULL, finished_at timestamptz
    )""")
    op.execute('CREATE INDEX ix_agent_runs_recent ON agent_runs (agent, created_at DESC)')
    op.execute("""CREATE TABLE agent_logs (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent_runs(id), organization_id uuid REFERENCES organizations(id),
        level varchar(12) NOT NULL CHECK (level IN ('INFO','WARNING','ERROR')), message varchar(500) NOT NULL, created_at timestamptz NOT NULL
    )""")
    op.execute('CREATE INDEX ix_agent_logs_recent ON agent_logs (created_at DESC, id DESC)')
    op.execute("""CREATE TABLE technical_issues (
        id uuid PRIMARY KEY, organization_id uuid REFERENCES organizations(id), fingerprint varchar(200) NOT NULL UNIQUE,
        kind varchar(40) NOT NULL, severity varchar(12) NOT NULL CHECK (severity IN ('WARNING','CRITICAL')),
        title varchar(160) NOT NULL, description varchar(500) NOT NULL,
        status varchar(24) NOT NULL CHECK (status IN ('NEEDS_ADMIN','AWAITING_APPROVAL','APPROVED','APPLYING','FIXED','FAILED','REJECTED','SUPERSEDED')),
        action varchar(24) CHECK (action IN ('RETRY_ORDER_AGENT','RETRY_MAILBOX')),
        target_id uuid, target_version integer, approved_by uuid REFERENCES users(id),
        automatic boolean NOT NULL, fixed_count integer NOT NULL, version integer NOT NULL,
        created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
    )""")
    op.execute('CREATE INDEX ix_technical_issues_recent ON technical_issues (status, created_at DESC)')
    op.execute("""CREATE TABLE runtime_diagnostics (
        id uuid PRIMARY KEY, organization_id uuid REFERENCES organizations(id),
        source varchar(10) NOT NULL CHECK (source IN ('API','WEB','DRIVER')),
        kind varchar(24) NOT NULL CHECK (kind IN ('HTTP_5XX','RUNTIME_ERROR','UNHANDLED_REJECTION')),
        bucket timestamptz NOT NULL, occurrences integer NOT NULL, last_seen_at timestamptz NOT NULL,
        CONSTRAINT runtime_diagnostic_window UNIQUE NULLS NOT DISTINCT (organization_id, source, kind, bucket)
    )""")
    for table in ('agent_runs', 'agent_logs', 'technical_issues', 'runtime_diagnostics'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY owner_access ON {table} USING ({OWNER}) WITH CHECK ({OWNER})')
    op.execute('GRANT SELECT, INSERT ON agent_runs, agent_logs, technical_issues, runtime_diagnostics TO dispatra_app')
    op.execute('GRANT UPDATE (status, step, progress, heartbeat_at, finished_at) ON agent_runs TO dispatra_app')
    op.execute('GRANT UPDATE (status, description, approved_by, fixed_count, version, updated_at) ON technical_issues TO dispatra_app')
    op.execute('GRANT UPDATE (occurrences, last_seen_at) ON runtime_diagnostics TO dispatra_app')


def downgrade():
    for table in ('runtime_diagnostics', 'technical_issues', 'agent_logs', 'agent_runs'):
        op.execute(f'DROP TABLE {table}')
