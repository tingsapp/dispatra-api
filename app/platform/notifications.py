"""Platform-owner inbox: recipient-scoped reads and transactional administrative notices."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import func, select, text, tuple_, update
from app.database import context
from app.models import Organization, User, now
from app.events.models import Notification
from app.events.schemas import ReadAllView
from app.services import audit, once
from .schemas import PlatformNotificationView


OWNER_NOTICES = {
    'watch.update_requested': ('WARNING', 'Driver progress update requested', 'A reported ready time passed without recorded completion. Review the operational report in Monitor.'),
    'watch.detected': ('WARNING', 'Operational risk detected', 'The Operational agent found an order risk. Review the evidence in Monitor.'),
    'watch.escalated': ('CRITICAL', 'Operational risk escalated', 'An order risk has become urgent. Review it in Monitor.'),
    'watch.driver_replied': ('WARNING', 'Driver clarification received', 'A driver replied to the Operational agent. Review the report in Monitor.'),
    'technical.approval_required': ('CRITICAL', 'Technical repair needs approval', 'The Technical agent proposed a repair. Review it in Monitor before it runs.'),
    'technical.problem_detected': ('CRITICAL', 'Technical problem reported', 'The Technical agent found a problem requiring investigation. Review it in Monitor.'),
    'settings.updated': ('INFO', 'Company settings updated', 'The dispatcher updated company settings.'),
    'password.changed': ('INFO', 'Dispatcher password changed', 'The dispatcher changed their account password.'),
    'issue.reported': ('CRITICAL', 'Operational issue reported', 'An operational issue was reported. Ask the company dispatcher to investigate.'),
    'mailbox.failed': ('CRITICAL', 'Order mailbox disconnected', 'The company order mailbox is disconnected. Ask the dispatcher to check it.'),
    'email.unknown': ('CRITICAL', 'Email delivery status unknown', 'A quote email may have been delivered. Ask the dispatcher to confirm before sending again.'),
}


def notify_owner(db, actor, action, organization_id, entity_id, actor_type='USER'):
    if not organization_id or action not in OWNER_NOTICES: return
    if action in {'settings.updated', 'password.changed'} and getattr(actor, 'role', None) != 'DISPATCHER': return
    if action == 'email.unknown':
        from app.operations.models import EmailDelivery
        delivery = db.get(EmailDelivery, entity_id)
        if delivery is None or delivery.notification_id: return  # Order-update uncertainty is not a critical quote alert.
    db.flush()
    saved = db.execute(text("SELECT current_setting('app.organization_id', true), current_setting('app.platform', true), "
        "current_setting('app.shipper_id', true), current_setting('app.driver_id', true)")).one()
    try:
        # Lookup only the owner ID in neutral account scope, then write one notice in its company scope.
        # Flush first so other pending writes never run under this internal notification context.
        context(db)
        recipient = db.scalar(select(User.id).where(User.role == 'ADMIN', User.active.is_(True)))
        if recipient is None or (actor_type == 'USER' and recipient == actor.id): return
        context(db, organization_id)
        severity, title, body = OWNER_NOTICES[action]
        if action.startswith('watch.'):
            from app.order_watch.models import Incident
            incident = db.get(Incident, entity_id)
            if incident: severity = incident.severity
        db.add(Notification(id=uuid4(), organization_id=organization_id, recipient_user_id=recipient,
            kind=action, title=title, body=body, severity=severity))
        db.flush()
    finally:
        if db.is_active:
            context(db, saved[0] or None, saved[1] == 'true', saved[2] or None, saved[3] or None)


def inbox(user):
    return select(Notification).where(Notification.recipient_user_id == user.id)


def view(db, row):
    return PlatformNotificationView.model_validate({**{field: getattr(row, field) for field in
        ('id', 'version', 'created_at', 'kind', 'severity', 'title', 'body', 'read_at', 'organization_id')},
        'company_name': db.scalar(select(Organization.name).where(Organization.id == row.organization_id)),
        'target': 'operational' if row.kind.startswith('watch.') or row.kind == 'issue.reported' else 'monitor' if row.kind.startswith('technical.') else 'company'}).model_dump(mode='json')


def page(db, user, limit, before=None):
    query = inbox(user)
    if before:
        anchor = db.scalar(query.where(Notification.id == before))
        if anchor is None: raise HTTPException(404, 'Notification not found.')
        query = query.where(tuple_(Notification.created_at, Notification.id) < (anchor.created_at, anchor.id))
    # Fetch company labels in the same query rather than one query per notification.
    rows = db.execute(query.add_columns(Organization.name).join(Organization, Organization.id == Notification.organization_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit))
    items = [PlatformNotificationView.model_validate({**{field: getattr(row, field) for field in
        ('id', 'version', 'created_at', 'kind', 'severity', 'title', 'body', 'read_at', 'organization_id')}, 'company_name': name,
        'target': 'operational' if row.kind.startswith('watch.') or row.kind == 'issue.reported' else 'monitor' if row.kind.startswith('technical.') else 'company'}) for row, name in rows]
    unread = db.scalar(select(func.count()).select_from(inbox(user).where(Notification.read_at.is_(None)).subquery()))
    return {'items': items, 'unread': unread}


def mark_read(db, user, identity, data, key):
    def run():
        row = db.scalar(inbox(user).where(Notification.id == identity).with_for_update())
        if row is None: raise HTTPException(404, 'Notification not found.')
        if not row.read_at:
            if row.version != data.version: raise HTTPException(409, 'Notification changed. Reload before continuing.')
            row.read_at = row.updated_at = now()
            row.version += 1
            audit(db, user, 'platform_owner.notification_read', row.id, row.organization_id)
            db.flush()
        return view(db, row)
    return once(db, user, key, f'platform-notification-read:{identity}', data.model_dump(), run)


def mark_all_read(db, user, key):
    def run():
        count = db.execute(update(Notification).where(Notification.recipient_user_id == user.id, Notification.read_at.is_(None))
            .values(read_at=now(), version=Notification.version + 1, updated_at=now())).rowcount
        if count: audit(db, user, 'platform_owner.notifications_read_all', user.id, None)
        return ReadAllView(updated=count).model_dump()
    return once(db, user, key, 'platform-notifications-read-all', {}, run)
