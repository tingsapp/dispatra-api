"""Which events become inbox notifications, for whom, and with what text.

Text never contains addresses, contact details, prices or private dispatcher notes; drivers and
Shippers see Order numbers only. Clients reauthorize through normal reads before opening a record.
"""
from dataclasses import dataclass
from sqlalchemy import select
from app.models import User


@dataclass(frozen=True)
class Note:
    user: User
    severity: str
    title: str
    body: str


class Recipients:
    """Active accounts per audience, loaded at most once per commit."""
    def __init__(self, db, organization_id):
        self.db, self.organization_id, self._dispatchers = db, organization_id, None

    def dispatchers(self):
        if self._dispatchers is None:
            self._dispatchers = list(self.db.scalars(select(User).where(User.organization_id == self.organization_id,
                User.role == 'DISPATCHER', User.active.is_(True)).order_by(User.created_at, User.id)))
        return self._dispatchers

    def account(self, column, value):
        if value is None: return []
        user = self.db.scalar(select(User).where(User.organization_id == self.organization_id, column == value, User.active.is_(True)))
        return [user] if user else []

    def shipper(self, audience): return self.account(User.shipper_id, audience.shipper_id)

    def driver(self, audience): return self.account(User.driver_id, audience.driver_id)


def _order(db, audience):
    from app.operations.models import Order
    return db.get(Order, audience.order_id) if audience.order_id else None


def notes(db, action, entity_id, audience, actor, recipients):
    from app.operations import models as m
    order = _order(db, audience)
    number = order.number if order else ''
    by_shipper = actor.role == 'SHIPPER'
    dispatch, shipper, driver = recipients.dispatchers, lambda: recipients.shipper(audience), lambda: recipients.driver(audience)

    def to(users, severity, title, body): return [Note(user, severity, title, body) for user in users]

    if action == 'order.created' and order:
        result = []
        if actor.kind == 'AGENT':
            owner = db.get(m.Shipper, order.shipper_id)
            result += to(dispatch(), 'INFO', 'New order from email', f'{number} was created from an email by {owner.name if owner else "a Shipper"}.')
            result += to(shipper(), 'INFO', 'Order received', f'{number} was created from your email.')
        elif by_shipper:
            owner = db.get(m.Shipper, order.shipper_id)
            result += to(dispatch(), 'INFO', 'New order booked', f'{number} was booked by {owner.name if owner else "a Shipper"}.')
        if order.pricing.get('status') != 'PRICED':
            result += to(dispatch(), 'WARNING', 'Price review needed', f'{number} needs a price review before dispatch.')
        return result
    if action in {'order.revised', 'order.cancelled'} and order:
        verb = 'changed' if action == 'order.revised' else 'cancelled'
        title, severity = ('Order updated', 'INFO') if action == 'order.revised' else ('Order cancelled', 'WARNING')
        if by_shipper: return to(dispatch(), severity, title, f'{number} was {verb} by the Shipper.')
        return to(shipper(), severity, title, f'{number} was {verb} by dispatch.')
    if action == 'order.assigned' and order:
        return (to(driver(), 'INFO', 'Order assigned', f'{number} is ready. Open Orders to view the latest stop sequence.')
            + to(shipper(), 'INFO', 'Driver assigned', f'{number} has been assigned to a driver.'))
    if action == 'order.unassigned' and order:
        return to(shipper(), 'INFO', 'Assignment changed', f'{number} is waiting for a new driver.')
    if action == 'route.released':
        return to(driver(), 'INFO', 'Assignment changed', 'A planned route was removed. Refresh Orders before starting your next stop.')
    if action == 'order.started' and order:
        return to(shipper(), 'INFO', 'Order in progress', f'The driver has started the route for {number}.')
    if action in {'order.completed', 'order.completed_by_dispatcher'} and order:
        return (to(shipper(), 'INFO', 'Order delivered', f'{number} was delivered. Proof of delivery is available.')
            + to(dispatch(), 'INFO', 'Order delivered', f'{number} was delivered.'))
    if action == 'order.completion_held' and order:
        return to(dispatch(), 'WARNING', 'Completion on hold', f'{number} has an open issue and is waiting for review.')
    if action == 'issue.reported':
        from app.operations.tracking import ISSUE_LABELS
        issue = db.get(m.Issue, entity_id)
        label = ISSUE_LABELS.get(issue.kind, 'Issue reported') if issue else 'Issue reported'
        return to(dispatch(), 'CRITICAL', 'Issue reported', f'{number}: {label}.' if number else f'{label}.')
    if action == 'invoice.created':
        invoice = db.get(m.Invoice, entity_id)
        if invoice and order: return to(shipper(), 'INFO', 'Invoice issued', f'Invoice {invoice.number} for {number} is ready.')
    if action in {'email.failed', 'email.unknown'}:
        delivery = db.get(m.EmailDelivery, entity_id)
        if delivery and delivery.notification_id:
            # A possibly-duplicated order update is harmless; only a definite failure needs attention.
            if action == 'email.unknown': return []
            return to(dispatch(), 'WARNING', 'Order update email not sent', 'An order update email to a Shipper could not be delivered. Check Settings → Mailbox.')
        document = 'invoice' if delivery and delivery.invoice_id else 'quote'
        if action == 'email.failed':
            return to(dispatch(), 'WARNING', 'Email not sent', f'A {document} email could not be delivered. Review it and send again.')
        return to(dispatch(), 'CRITICAL', 'Email status unknown', f'A {document} email may have been delivered. Confirm with the recipient before sending again.')
    if action == 'intake.needs_review':
        from app.intake.models import EmailIntake
        intake = db.get(EmailIntake, entity_id)
        owner = db.get(m.Shipper, intake.shipper_id) if intake and intake.shipper_id else None
        return to(dispatch(), 'WARNING', 'Email order needs review', f'An email from {owner.name if owner else "a Shipper"} needs details before it can become an Order.')
    if action == 'intake.unknown_sender':
        return to(dispatch(), 'WARNING', 'Email from unknown sender', 'An order email from an address that is not a Shipper is waiting for review.')
    if action == 'intake.failed':
        return to(dispatch(), 'WARNING', 'Order email not processed', 'An email could not be read automatically. Review it in Settings → Mailbox.')
    if action == 'mailbox.failed':
        return to(dispatch(), 'CRITICAL', 'Order mailbox disconnected', 'Dispatra cannot read the order mailbox. Check the mailbox settings.')
    if action == 'dispatch.auto_assigned' and order:
        from app.dispatch.models import DispatchDecision
        decision = db.get(DispatchDecision, entity_id)
        chosen = db.get(m.Driver, decision.driver_id) if decision and decision.driver_id else None
        return to(dispatch(), 'INFO', 'Order auto-assigned', f'{number} was assigned to {chosen.name if chosen else "a driver"} by auto dispatch.')
    if action == 'dispatch.no_candidate' and order:
        return to(dispatch(), 'WARNING', 'No driver available', f'Auto dispatch could not find a driver for {number}. Open it to see why.')
    if action in {'duty.started_by_dispatcher', 'duty.ended_by_dispatcher'}:
        on = action == 'duty.started_by_dispatcher'
        return to(driver(), 'INFO', 'On duty' if on else 'Off duty', f'Dispatch set you {"on" if on else "off"} duty.')
    return []
