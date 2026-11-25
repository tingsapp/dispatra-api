"""Driver stop reasons and report photos on Issues and progress replies; delay-only reports stop holding completion."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0077_stop_report_reasons'
down_revision = '0076_password_reset'
branch_labels = None
depends_on = None


def upgrade():
    # Existing Issues keep holding completion, as they did when they were filed.
    op.add_column('operation_issues', sa.Column('reasons', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column('operation_issues', sa.Column('photo_ids', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column('operation_issues', sa.Column('holds_completion', sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column('order_watch_questions', sa.Column('reasons', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column('order_watch_questions', 'reasons')
    op.drop_column('operation_issues', 'holds_completion')
    op.drop_column('operation_issues', 'photo_ids')
    op.drop_column('operation_issues', 'reasons')
