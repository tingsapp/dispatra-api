"""Persist bounded Technical-agent assessments and successful browser recovery samples."""
from alembic import op
import sqlalchemy as sa

revision = '0060_technical_recovery'
down_revision = '0059_email_attachments'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('technical_issues', sa.Column('diagnosis', sa.String(500)))
    op.add_column('technical_issues', sa.Column('diagnosis_source', sa.String(12)))
    op.add_column('technical_issues', sa.Column('diagnosed_at', sa.DateTime(timezone=True)))
    op.add_column('technical_issues', sa.Column('notified_at', sa.DateTime(timezone=True)))
    op.create_check_constraint('technical_diagnosis_source', 'technical_issues', "diagnosis_source IS NULL OR diagnosis_source IN ('AI','RULES')")
    # Existing reports have already been surfaced; don't send a fresh notification storm.
    op.execute("UPDATE technical_issues SET notified_at = updated_at")
    op.execute('GRANT UPDATE (diagnosis, diagnosis_source, diagnosed_at, notified_at) ON technical_issues TO dispatra_app')
    op.drop_constraint('runtime_diagnostics_kind_check', 'runtime_diagnostics', type_='check')
    op.create_check_constraint('runtime_diagnostics_kind_check', 'runtime_diagnostics',
        "kind IN ('HTTP_5XX','RUNTIME_ERROR','UNHANDLED_REJECTION','READ_RECOVERED','READ_RECOVERY_FAILED')")


def downgrade():
    op.execute("DELETE FROM runtime_diagnostics WHERE kind IN ('READ_RECOVERED','READ_RECOVERY_FAILED')")
    op.drop_constraint('runtime_diagnostics_kind_check', 'runtime_diagnostics', type_='check')
    op.create_check_constraint('runtime_diagnostics_kind_check', 'runtime_diagnostics', "kind IN ('HTTP_5XX','RUNTIME_ERROR','UNHANDLED_REJECTION')")
    op.drop_constraint('technical_diagnosis_source', 'technical_issues', type_='check')
    for name in ('notified_at', 'diagnosed_at', 'diagnosis_source', 'diagnosis'):
        op.drop_column('technical_issues', name)
