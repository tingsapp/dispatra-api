"""Local isolated code-repair progress and privacy-limited fault locations."""
from alembic import op
import sqlalchemy as sa

revision = '0061_local_code_repair'
down_revision = '0060_technical_recovery'
branch_labels = depends_on = None


def upgrade():
    op.add_column('technical_issues', sa.Column('repair_stage', sa.String(24)))
    op.add_column('technical_issues', sa.Column('repair_summary', sa.String(500)))
    op.add_column('runtime_diagnostics', sa.Column('code_path', sa.String(240)))
    op.add_column('runtime_diagnostics', sa.Column('code_line', sa.Integer()))
    op.add_column('runtime_diagnostics', sa.Column('error_type', sa.String(80)))
    op.drop_constraint('runtime_diagnostic_window', 'runtime_diagnostics', type_='unique')
    op.execute('ALTER TABLE runtime_diagnostics ADD CONSTRAINT runtime_diagnostic_window UNIQUE NULLS NOT DISTINCT (organization_id, source, kind, bucket, code_path, code_line, error_type)')
    op.execute('GRANT UPDATE (repair_stage, repair_summary, automatic) ON technical_issues TO dispatra_app')
    op.execute('GRANT UPDATE (code_path, code_line, error_type) ON runtime_diagnostics TO dispatra_app')


def downgrade():
    op.execute("UPDATE technical_issues SET status='NEEDS_ADMIN', notified_at=NULL WHERE repair_stage IS NOT NULL AND status='APPLYING'")
    for name in ('repair_stage', 'repair_summary'): op.drop_column('technical_issues', name)
    op.drop_constraint('runtime_diagnostic_window', 'runtime_diagnostics', type_='unique')
    # Consolidate the finer fault groups without losing their counts when reverting the schema.
    op.execute('''WITH groups AS (
        SELECT (array_agg(id ORDER BY id))[1] AS keeper, sum(occurrences) AS total, max(last_seen_at) AS latest
        FROM runtime_diagnostics GROUP BY organization_id, source, kind, bucket)
        UPDATE runtime_diagnostics d SET occurrences=g.total, last_seen_at=g.latest FROM groups g WHERE d.id=g.keeper''')
    op.execute('''DELETE FROM runtime_diagnostics d USING runtime_diagnostics k WHERE d.id>k.id
        AND d.organization_id IS NOT DISTINCT FROM k.organization_id AND d.source=k.source AND d.kind=k.kind AND d.bucket=k.bucket''')
    for name in ('code_path', 'code_line', 'error_type'): op.drop_column('runtime_diagnostics', name)
    op.execute('ALTER TABLE runtime_diagnostics ADD CONSTRAINT runtime_diagnostic_window UNIQUE NULLS NOT DISTINCT (organization_id, source, kind, bucket)')
