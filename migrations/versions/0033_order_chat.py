"""Order chat: up to three private two-party conversations per Order and per-account read positions.

`shipper_id`/`driver_id` hold the participants only (NULL for the absent party), so the shared
Shipper/driver RLS predicate admits exactly the conversations and messages that party is in. The API
additionally requires the driver to still be the Order's current driver. Messages are immutable.
"""
from alembic import op

revision = '0033_order_chat'
down_revision = '0032_api_keys'
branch_labels = None
depends_on = None

SCOPED = """(organization_id::text = nullif(current_setting('app.organization_id', true), '') OR current_setting('app.platform', true) = 'true')
    AND (coalesce(current_setting('app.shipper_id', true), '') = '' OR shipper_id::text = current_setting('app.shipper_id', true))
    AND (coalesce(current_setting('app.driver_id', true), '') = '' OR driver_id::text = current_setting('app.driver_id', true))"""


def upgrade():
    op.execute("""CREATE TABLE conversations (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        order_id uuid NOT NULL,
        kind varchar(20) NOT NULL,
        shipper_id uuid,
        driver_id uuid,
        last_message_at timestamptz,
        created_at timestamptz NOT NULL,
        CONSTRAINT conversation_participants CHECK ((kind = 'DISPATCH_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NULL)
            OR (kind = 'DISPATCH_DRIVER' AND shipper_id IS NULL AND driver_id IS NOT NULL)
            OR (kind = 'DRIVER_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NOT NULL)),
        CONSTRAINT conversations_organization_id_id_key UNIQUE (organization_id, id),
        CONSTRAINT conversations_order_kind_driver UNIQUE NULLS NOT DISTINCT (organization_id, order_id, kind, driver_id),
        FOREIGN KEY (organization_id, order_id) REFERENCES orders(organization_id, id),
        FOREIGN KEY (organization_id, shipper_id) REFERENCES shippers(organization_id, id),
        FOREIGN KEY (organization_id, driver_id) REFERENCES drivers(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_conversations_recent ON conversations (organization_id, last_message_at DESC)')
    op.execute("""CREATE TABLE conversation_messages (
        id uuid PRIMARY KEY,
        seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        organization_id uuid NOT NULL REFERENCES organizations(id),
        conversation_id uuid NOT NULL,
        shipper_id uuid,
        driver_id uuid,
        sender_user_id uuid NOT NULL REFERENCES users(id),
        sender_role varchar(12) NOT NULL,
        sender_name varchar(160) NOT NULL,
        body varchar(2000) NOT NULL,
        created_at timestamptz NOT NULL,
        CONSTRAINT message_sender_role CHECK (sender_role IN ('DISPATCHER','SHIPPER','DRIVER')),
        CONSTRAINT message_body CHECK (length(btrim(body)) > 0),
        FOREIGN KEY (organization_id, conversation_id) REFERENCES conversations(organization_id, id)
    )""")
    op.execute('CREATE INDEX ix_conversation_messages_thread ON conversation_messages (conversation_id, seq)')
    op.execute("""CREATE TABLE conversation_reads (
        organization_id uuid NOT NULL REFERENCES organizations(id),
        conversation_id uuid NOT NULL,
        user_id uuid NOT NULL REFERENCES users(id),
        shipper_id uuid,
        driver_id uuid,
        last_read_seq bigint NOT NULL,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (conversation_id, user_id),
        FOREIGN KEY (organization_id, conversation_id) REFERENCES conversations(organization_id, id)
    )""")
    for table in ('conversations', 'conversation_messages', 'conversation_reads'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} USING ({SCOPED}) WITH CHECK ({SCOPED})')
    op.execute('GRANT SELECT, INSERT ON conversations, conversation_messages, conversation_reads TO dispatra_app')
    op.execute('GRANT UPDATE (last_message_at) ON conversations TO dispatra_app')
    op.execute('GRANT UPDATE (last_read_seq, updated_at) ON conversation_reads TO dispatra_app')


def downgrade():
    op.execute('DROP TABLE conversation_reads')
    op.execute('DROP TABLE conversation_messages')
    op.execute('DROP TABLE conversations')
