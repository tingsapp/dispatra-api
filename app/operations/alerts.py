"""Hiding computed Needs Attention warnings; reported issues are resolved through `issues.resolve`."""
from sqlalchemy import select
from .common import command, record
from .models import AttentionDismissal, Order
from app.services import audit


def hide(db, actor, rows, key):
    """Hide each listed warning while its Order keeps the version it had on the dispatcher's card."""
    def run():
        for item in rows:
            record(db, Order, actor, item.order_id)
            row = db.scalar(select(AttentionDismissal).where(AttentionDismissal.organization_id == actor.organization_id,
                AttentionDismissal.order_id == item.order_id, AttentionDismissal.key == item.key).with_for_update())
            if row is None:
                db.add(AttentionDismissal(organization_id=actor.organization_id, order_id=item.order_id, key=item.key,
                    order_version=item.order_version, severity=item.severity, dismissed_by=actor.id))
            else:
                row.order_version, row.severity, row.dismissed_by, row.version = item.order_version, item.severity, actor.id, row.version + 1
        db.flush()
        # A `watch.*` event refreshes every open Monitor's Needs Attention.
        if rows: audit(db, actor, 'watch.hidden', rows[0].order_id, actor.organization_id)
        return {'updated': len(rows)}
    return command(db, actor, key, 'attention-hide', [item.model_dump(mode='json') for item in rows], run)
