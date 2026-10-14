"""Allow durable Chat agent monitoring, preserving existing histories."""
from alembic import op

revision = '0049_chat_agent'
down_revision = '0048_workspace_rate'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_agent_check')
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_agent_check CHECK (agent IN ('ORDER','DISPATCH','TECHNICAL','OPERATIONAL','CHAT'))")


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_runs WHERE agent='CHAT') THEN RAISE EXCEPTION 'Archive Chat run history before rollback'; END IF; END $$")
    op.execute('ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_agent_check')
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_agent_check CHECK (agent IN ('ORDER','DISPATCH','TECHNICAL','OPERATIONAL'))")
