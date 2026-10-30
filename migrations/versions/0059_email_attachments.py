"""Encrypted email attachments; existing emails remain unchanged."""
from alembic import op
import sqlalchemy as sa
revision='0059_email_attachments'
down_revision='0058_samsara_connection'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('email_intakes',sa.Column('attachment_secret',sa.Text(),nullable=True))

def downgrade():
    op.drop_column('email_intakes','attachment_secret')
