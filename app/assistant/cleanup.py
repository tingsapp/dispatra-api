"""Chat clean-up cards: the dispatcher's notifications and the company's Needs Attention list.

Notifications can be marked read or deleted. Reported issues are resolved through the Resolve command; computed order
warnings and Operational incidents cannot be deleted (they are recalculated), so clearing hides each one until its
Order changes or the incident gets worse. Incoming e-mail/TMS requests are never discarded by clearing alerts."""
from sqlalchemy import func, select
from app.events.routes import unread
from app.models import now
from app.operations import alerts, issues
from app.operations.models import Order
from app.operations.reporting import open_attention
from app.operations.schemas import IssueBatch
from . import drafting, records
from .cards import reply, proposal, reference

SHOWN = 10  # item lines on a bulk card; the rest are counted
DEFAULT_NOTE = 'Resolved from Dispatra chat.'
LABELS = {'PRICING': 'price review', 'LATE_START': 'late start', 'AT_RISK': 'delivery at risk', 'NO_DRIVER': 'no driver', 'DUE': 'pickup due'}


def notifications(db, actor, name, op):
    count = unread(db, actor) if name == 'NOTIFICATION.READ_ALL' else db.scalar(select(func.count()).select_from(
        records.notifications(actor).subquery()))
    if name == 'NOTIFICATION.READ_ALL':
        if not count: return reply('All your notifications are already read.')
        return proposal(name, op, 'your notifications', {}, [{'field': 'Unread notifications', 'before': str(count), 'after': '0'},
            {'field': 'Note', 'after': 'They stay in your inbox as read history.'}])
    if not count: return reply('You have no notifications to delete.')
    return proposal(name, op, 'your notifications', {}, [{'field': 'Notifications', 'before': str(count), 'after': 'Deleted'},
        {'field': 'Note', 'after': 'This can’t be undone. Ones already sent to you by e-mail are kept.'}])


def capped(lines, noun):
    if len(lines) <= SHOWN: return lines
    return lines[:SHOWN] + [{'field': 'More', 'after': f'{len(lines) - SHOWN} more {noun}'}]


def note(plan):
    text = plan.request.strip()[:2000]
    return text if len(text) >= 3 else DEFAULT_NOTE


def order_scope(db, actor, plan):
    """(order, references, refusal) for a request limited to one named Order; no Order when none is named."""
    term = plan.target.strip()
    if not term: return None, [], None
    found = records.find(db, actor.organization_id, 'ORDER', term)
    if len(found) != 1:
        return None, [], reply(f'I couldn’t find an order matching “{term}”.' if not found else f'More than one order matches “{term}”. Which one?',
            [ref for row in found for ref in reference('ORDER', row)])
    return found[0], reference('ORDER', found[0]), None


def numbers(db, actor, ids):
    if not ids: return {}
    return dict(db.execute(select(Order.id, Order.number).where(Order.organization_id == actor.organization_id, Order.id.in_(ids))).all())


def issue_lines(rows, number):
    return [{'field': f'{number.get(row.order_id, "Order")} {row.kind.replace("_", " ").lower()}', 'before': 'Open', 'after': 'Resolved'} for row in rows]


def resolve_issues(db, actor, name, op, plan):
    """Every open reported issue in the company, or on one named Order, closed with one note after confirmation."""
    order, refs, refusal = order_scope(db, actor, plan)
    if refusal: return refusal
    rows = records.open_issues(db, actor.organization_id, order)
    if not rows: return reply(f'{f"Order {order.number} has" if order else "There are"} no open reported issues.', refs)
    body = drafting.validate(op.body, {'issues': [{'id': str(row.id), 'version': row.version} for row in rows], 'resolution': note(plan)})
    lines = capped(issue_lines(rows, numbers(db, actor, {row.order_id for row in rows})), 'issues') + [{'field': 'Resolution', 'after': body['resolution']}]
    subject = f'{len(rows)} open issue{"" if len(rows) == 1 else "s"}' + (f' on order {order.number}' if order else '')
    return proposal(name, op, subject, body, lines, references=refs)


def clear_alerts(db, actor, name, op, plan):
    """Everything in Needs Attention the dispatcher can clear: reported issues and visible order warnings."""
    order, refs, refusal = order_scope(db, actor, plan)
    if refusal: return refusal
    orders, attention = open_attention(db, actor.organization_id, now())
    rows = records.open_issues(db, actor.organization_id, order)
    warnings = [(orders[order_id], row) for order_id, items in attention.items() for row in items
        if not row['hidden'] and (order is None or order_id == order.id)]
    waiting = records.waiting_requests(db, actor.organization_id)
    if not rows and not warnings:
        return reply(f'{f"Order {order.number} has" if order else "There is"} nothing to clear in Needs Attention.'
            + (f' {waiting} incoming request{"s" if waiting != 1 else ""} still need review.' if waiting and not order else ''), refs)
    number = numbers(db, actor, {row.order_id for row in rows})
    lines = issue_lines(rows, number) + [{'field': f'{item.number} {LABELS.get(row["key"]) or row["key"].partition(":")[2].replace("_", " ").lower() or "alert"}',
        'before': 'Showing', 'after': 'Hidden until the order changes'} for item, row in warnings]
    body = drafting.validate(op.body, {'issues': [{'id': str(row.id), 'version': row.version} for row in rows], 'resolution': note(plan),
        'hide': [{'order_id': str(item.id), 'key': row['key'], 'order_version': item.version, 'severity': row['severity']} for item, row in warnings]})
    lines = capped(lines, 'alerts')
    if rows: lines.append({'field': 'Resolution', 'after': body['resolution']})
    if waiting and not order:
        lines.append({'field': 'Not included', 'after': f'{waiting} incoming request{"s" if waiting != 1 else ""} waiting for review; ask me to discard them'})
    count = len(rows) + len(warnings)
    return proposal(name, op, f'{count} alert{"" if count == 1 else "s"}' + (f' on order {order.number}' if order else ''), body, lines, references=refs)


def run_clear(db, actor, body, key):
    """Confirmed ALERT.CLEAR_ALL: hide the warnings, then resolve each reported issue with its own version check."""
    hidden = alerts.hide(db, actor, body.hide, key)['updated'] if body.hide else 0
    if body.issues: issues.resolve_many(db, actor, IssueBatch(issues=[item.model_dump() for item in body.issues], resolution=body.resolution), key)
    return {'updated': hidden + len(body.issues)}
