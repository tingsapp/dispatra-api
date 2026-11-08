"""Dispatcher inbox and private admin/dispatcher conversations; preserve existing history."""
from alembic import op
from sqlalchemy import text
revision = '0064_dispatcher_chat'
down_revision = '0063_shipper_logo'
branch_labels = depends_on = None


def upgrade():
    op.execute('ALTER TABLE users ADD CONSTRAINT users_chat_tenant UNIQUE (organization_id,id)')
    op.execute('ALTER TABLE conversations ADD COLUMN dispatcher_id uuid')
    op.execute('ALTER TABLE conversations ADD CONSTRAINT conversation_dispatcher_tenant FOREIGN KEY (organization_id,dispatcher_id) REFERENCES users(organization_id,id)')
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversation_participants')
    op.execute("""ALTER TABLE conversations ADD CONSTRAINT conversation_participants CHECK (
        (kind='ADMIN_DISPATCHER' AND dispatcher_id IS NOT NULL AND shipper_id IS NULL AND driver_id IS NULL AND order_id IS NULL)
        OR (dispatcher_id IS NULL AND ((kind='DISPATCH_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NULL)
        OR (kind='DISPATCH_DRIVER' AND shipper_id IS NULL AND driver_id IS NOT NULL)
        OR (kind='DRIVER_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NOT NULL))))""")
    op.execute('ALTER TABLE conversations DROP CONSTRAINT company_chat_kind')
    op.execute("ALTER TABLE conversations ADD CONSTRAINT company_chat_kind CHECK (order_id IS NOT NULL OR kind IN ('DISPATCH_DRIVER','DISPATCH_SHIPPER','ADMIN_DISPATCHER'))")
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversations_order_kind_driver')
    op.execute('ALTER TABLE conversations ADD CONSTRAINT conversations_order_kind_driver UNIQUE NULLS NOT DISTINCT (organization_id, order_id, kind, driver_id, shipper_id, dispatcher_id)')
    op.execute('ALTER TABLE conversation_messages DROP CONSTRAINT message_sender_role')
    op.execute("ALTER TABLE conversation_messages ADD CONSTRAINT message_sender_role CHECK (sender_role IN ('ADMIN','DISPATCHER','SHIPPER','DRIVER'))")
    op.execute('ALTER TABLE notifications ADD COLUMN conversation_id uuid')
    op.execute('ALTER TABLE notifications ADD CONSTRAINT notification_conversation FOREIGN KEY (organization_id, conversation_id) REFERENCES conversations(organization_id,id)')
    op.execute("""UPDATE notifications n SET conversation_id=m.conversation_id FROM events e JOIN conversation_messages m ON m.id=e.entity_id AND m.organization_id=e.organization_id
        WHERE n.event_id=e.id AND n.organization_id=e.organization_id AND n.kind='message.created'""")
    op.execute('CREATE INDEX ix_notifications_conversation ON notifications (organization_id, recipient_user_id, conversation_id) WHERE conversation_id IS NOT NULL')


def downgrade():
    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM conversations WHERE kind='ADMIN_DISPATCHER')")):
        raise RuntimeError('Admin chat history exists; refusing a destructive downgrade.')
    op.execute('DROP INDEX ix_notifications_conversation')
    op.execute('ALTER TABLE notifications DROP COLUMN conversation_id')
    op.execute('ALTER TABLE conversation_messages DROP CONSTRAINT message_sender_role')
    op.execute("ALTER TABLE conversation_messages ADD CONSTRAINT message_sender_role CHECK (sender_role IN ('DISPATCHER','SHIPPER','DRIVER'))")
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversations_order_kind_driver')
    op.execute('ALTER TABLE conversations ADD CONSTRAINT conversations_order_kind_driver UNIQUE NULLS NOT DISTINCT (organization_id, order_id, kind, driver_id, shipper_id)')
    op.execute('ALTER TABLE conversations DROP CONSTRAINT company_chat_kind')
    op.execute("ALTER TABLE conversations ADD CONSTRAINT company_chat_kind CHECK (order_id IS NOT NULL OR kind IN ('DISPATCH_DRIVER','DISPATCH_SHIPPER'))")
    op.execute('ALTER TABLE conversations DROP CONSTRAINT conversation_participants')
    op.execute("""ALTER TABLE conversations ADD CONSTRAINT conversation_participants CHECK ((kind='DISPATCH_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NULL)
        OR (kind='DISPATCH_DRIVER' AND shipper_id IS NULL AND driver_id IS NOT NULL)
        OR (kind='DRIVER_SHIPPER' AND shipper_id IS NOT NULL AND driver_id IS NOT NULL))""")
    op.execute('ALTER TABLE conversations DROP COLUMN dispatcher_id')
    op.execute('ALTER TABLE users DROP CONSTRAINT users_chat_tenant')
