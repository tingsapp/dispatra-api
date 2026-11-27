"""Chat record changes: the assistant drafts a change, the dispatcher confirms it, and the workspace command saves it.

A proposal's body is exactly that command's request, and confirmation runs the command as the signed-in dispatcher, so a
chat change can do nothing their own workspace session could not. The command re-checks company, version, state,
idempotency, audit and events; provider text never reaches a write without the dispatcher's confirmation."""
import json
import re
from datetime import datetime
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from app.models import now
from app.services import audit
from app.operations import orders
from app.operations.common import command, record, settings
from app.operations.models import Order, Route, Driver, Catalog, RateCard
from app.operations.schemas import Version
from . import cleanup, drafting, provider, records, workspace
from .cards import reply, ask, proposal, reference
from .search import company_zone

UNAVAILABLE = 'I can’t prepare changes right now. Try again in a moment, or make the change in the workspace.'
REFUSED = 'I can’t make that change from chat. Dispatcher accounts and billing are handled in the workspace.'
STATUS = {'NEW': 'Unassigned', 'ASSIGNED': 'Assigned', 'IN_PROGRESS': 'On the road', 'COMPLETED': 'Completed', 'CANCELLED': 'Cancelled'}
PLURAL_TARGETS = {'all', 'both', 'them', 'these', 'those', 'all of them', 'both of them', 'the two of them', 'two of them', 'all orders', 'both orders'}
ORDER_BATCH_EDIT_LIMIT = 10
ORDER_BATCH_CLOSE_LIMIT = 50
ORDER_NUMBER_PATTERN = re.compile(r'\b[A-Z]{2,6}-[A-Z0-9-]{2,20}\b')


def _selected_row(db, org, entity, context):
    """Resolve the active Monitor selection inside the company when the user omitted an explicit target."""
    if db is None or context is None: return None
    if entity in {'ORDER', 'ISSUE'} and getattr(context, 'selected_order_id', None):
        return db.scalar(select(Order).where(Order.organization_id == org, Order.id == context.selected_order_id, Order.archived_at.is_(None)))
    if entity == 'DRIVER' and getattr(context, 'selected_driver_id', None):
        return db.scalar(select(Driver).where(Driver.organization_id == org, Driver.id == context.selected_driver_id, Driver.archived_at.is_(None)))
    return None


def _split_targets(term):
    """Split comma/and-separated targets like 'DDO-3967, DDO-2067' or 'DDO-3967 and DDO-2067'."""
    parts = [part.strip() for part in re.split(r'\s*(?:,|;|&|\band\b)\s*', term.strip(), flags=re.I) if part.strip()]
    return list(dict.fromkeys(parts))


def _history_orders(db, org, history, archived=False):
    """Resolve orders mentioned in the most recent conversation turn that named company orders."""
    if db is None or not history: return []
    for turn in reversed(history):
        text = turn.content if hasattr(turn, 'content') else str(turn.get('content', ''))
        candidates = list(dict.fromkeys(ORDER_NUMBER_PATTERN.findall(text)))
        if not candidates: continue
        base = select(Order).where(Order.organization_id == org,
            Order.archived_at.is_not(None) if archived else Order.archived_at.is_(None),
            Order.number.in_(candidates))
        found = {row.number: row for row in db.scalars(base).all()}
        ordered = [found[num] for num in candidates if num in found]
        if ordered: return ordered
    return []


def _resolve_order_targets(db, org, term, verb, context, history):
    """Return `(rows, error_reply)` for one or many Orders, using conversation history for 'both'/'them'."""
    archived = verb == 'RESTORE'
    parts = _split_targets(term)
    if len(parts) > 1:
        rows, seen = [], set()
        for part in parts:
            found = records.find(db, org, 'ORDER', part, archived=archived)
            if not found:
                return [], reply(f'I couldn’t find {"an archived" if archived else "an"} order matching “{part}”.')
            if len(found) > 1:
                names = ', '.join(records.label('ORDER', row) for row in found)
                return [], reply(f'More than one order matches “{part}”: {names}. Which one do you mean?',
                    [ref for row in found for ref in reference('ORDER', row)])
            if found[0].id not in seen:
                seen.add(found[0].id); rows.append(found[0])
        return rows, None
    if not term or term.lower() in PLURAL_TARGETS:
        from_history = _history_orders(db, org, history, archived=archived)
        if from_history: return from_history, None
        if term.lower() in {'all', 'all orders', 'all of them'} and db is not None:
            limit = ORDER_BATCH_EDIT_LIMIT if verb == 'UPDATE' else ORDER_BATCH_CLOSE_LIMIT
            base = select(Order).where(Order.organization_id == org,
                Order.archived_at.is_not(None) if archived else Order.archived_at.is_(None))
            if verb == 'UPDATE': base = base.where(Order.status.in_(['NEW', 'ASSIGNED', 'IN_PROGRESS']))
            elif verb == 'CANCEL': base = base.where(Order.status == 'NEW')
            elif verb == 'COMPLETE': base = base.where(Order.status.in_(['ASSIGNED', 'IN_PROGRESS']))
            rows = list(db.scalars(base.order_by(Order.updated_at.desc(), Order.id).limit(limit + 1)).all())
            if rows: return rows, None
        selected = _selected_row(db, org, 'ORDER', context)
        if selected is not None: return [selected], None
        return [], reply('Which order? Tell me its number.')
    found = records.find(db, org, 'ORDER', term, archived=archived)
    if not found:
        return [], reply(f'I couldn’t find {"an archived" if archived else "a"} order matching “{term}”.')
    if len(found) > 1:
        names = ', '.join(records.label('ORDER', row) for row in found)
        return [], reply(f'More than one order matches “{term}”: {names}. Which one do you mean?',
            [ref for row in found for ref in reference('ORDER', row)])
    return found, None


def prepare(db, actor, plan, context=None, history=None):
    """Resolve the record the dispatcher named and draft the change without saving anything."""
    if plan.entity == 'REQUEST':
        try: return workspace.requests(db, actor, plan, records.operation)
        except drafting.Incomplete as error: return ask(error.problems, 'order')
    if plan.operation == 'UNASSIGN' and plan.entity in {'ORDER', 'DRIVER'}:
        name, op = records.operation('ORDER', 'UNASSIGN')
        return unassign_request(db, actor, name, op, plan, context)
    found = records.operation(plan.entity, plan.operation)
    if not found: return reply(REFUSED)
    name, op = found
    entity, verb = name.split('.')
    noun = records.NOUNS[entity]
    try:
        if name == 'ORDER.CREATE': return new_order(db, actor, plan, op)
        if verb == 'CREATE': return create(db, actor, entity, name, op, plan)
        if entity == 'SETTINGS': return update(db, actor, entity, name, op, plan, settings(db, actor)[0], 'company settings')
        if entity == 'NOTIFICATION': return cleanup.notifications(db, actor, name, op)
        if name == 'ISSUE.RESOLVE_ALL': return cleanup.resolve_issues(db, actor, name, op, plan)
        if name == 'ALERT.CLEAR_ALL': return cleanup.clear_alerts(db, actor, name, op, plan)
        if name == 'QUOTE.SEND': return workspace.send_quote(db, actor, name, op, plan)
        if entity == 'CONNECTOR': return workspace.connector(db, actor, name, op, plan)
        if entity in {'TMS', 'MAILBOX'}:
            row = records.company_record(db, actor.organization_id, entity)
            if row is None: return reply(f'The {noun} isn’t set up yet. Set it up in Integrations first; its password or key can’t be entered in chat.')
            if verb == 'SYNC': return proposal(name, op, f'the {noun}', {}, [{'field': 'Drivers and vehicles', 'after': 'Imported from the TMS now'}])
            return update(db, actor, entity, name, op, plan, row, f'the {noun}')
        if entity == 'ISSUE': return resolve_issue(db, actor, name, op, plan, context)
        term = plan.target.strip()
        if entity == 'ORDER' and verb in {'UPDATE', 'CANCEL', 'ARCHIVE', 'COMPLETE', 'RESTORE'}:
            rows, refusal = _resolve_order_targets(db, actor.organization_id, term, verb, context, history)
            if refusal: return refusal
            if len(rows) > 1:
                return update_many(db, actor, op, plan, rows) if verb == 'UPDATE' else close_many(db, actor, verb, rows)
            row = rows[0]
            return update(db, actor, entity, name, op, plan, row, records.label(entity, row)) if verb == 'UPDATE' else close(db, actor, entity, name, op, row)
        if not term:
            selected = _selected_row(db, actor.organization_id, entity, context)
            if selected is None: return reply(f'Which {noun}? Tell me its {"number" if entity == "ORDER" else "name"}.')
            rows = [selected]
        else:
            rows = records.find(db, actor.organization_id, entity, term, archived=verb == 'RESTORE')
        if not rows: return reply(f'I couldn’t find {"an archived" if verb == "RESTORE" else "a"} {noun} matching “{term}”.')
        if len(rows) > 1:
            names = ', '.join(records.label(entity, row) for row in rows)
            return reply(f'More than one {noun} matches “{term}”: {names}. Which one do you mean?',
                [ref for row in rows for ref in reference(entity, row)])
        row = rows[0]
        if verb == 'UPDATE': return update(db, actor, entity, name, op, plan, row, records.label(entity, row))
        if verb == 'UNASSIGN': return unassign(db, actor, name, op, row)
        if verb == 'RESET_PASSWORD': return workspace.reset_password(db, actor, entity, name, op, row)
        return close(db, actor, entity, name, op, row)
    except drafting.Incomplete as error: return ask(error.problems, noun)
    except provider.Unavailable: return reply(UNAVAILABLE)


def context(db, actor):
    _, config = settings(db, actor)
    zone = company_zone(db, actor.organization_id)
    catalog = db.execute(select(Catalog.kind, Catalog.code, Catalog.data['name'].astext).where(
        Catalog.organization_id == actor.organization_id, Catalog.active.is_(True))).all()
    cards = db.scalars(select(RateCard.data['name'].astext).where(RateCard.organization_id == actor.organization_id, RateCard.active.is_(True))).all()
    return zone, {'company_time_zone': str(zone), 'company_local_now': datetime.now(zone).replace(microsecond=0, tzinfo=None).isoformat(),
        'company_units': {'weight': config.weight_unit, 'dimension': config.dimension_unit, 'distance': config.distance_unit},
        'catalogue': {kind.lower(): [name or code for k, code, name in catalog if k == kind] for kind in records.CATALOG},
        'rate_cards': list(cards)}


def schema(op):
    """The request schema as the AI should write it: names for linked records and one-line addresses, no version or secrets."""
    shape = op.body.model_json_schema()
    for field in ('version', *workspace.SECRETS): shape.get('properties', {}).pop(field, None)
    shape['required'] = [field for field in shape.get('required', []) if field != 'version' and field not in workspace.SECRETS]
    for name, definition in [('', shape), *shape.get('$defs', {}).items()]:
        for key, field in definition.get('properties', {}).items():
            entity = 'ACCESSORIAL' if name == 'AccessorialSelection' and key == 'id' else drafting.REFERENCES.get(key)
            if entity:
                optional = 'null' in json.dumps(field)
                field.clear(); field.update(type=['string', 'null'] if optional else 'string',
                    description=f'Name or code of the {records.NOUNS[entity]}, exactly as listed or written')
            elif key in drafting.ADDRESSES and 'Address' in json.dumps(field):
                field.clear(); field.update(type='string', description='One line: street, city, province, postal code')
    return shape


def normalized(db, actor, op, body):
    """Body as the workspace schema would save it, with names for display; raw when legacy data no longer validates."""
    try: body = drafting.validate(op.body, body)
    except drafting.Incomplete: pass
    return drafting.humanize(db, actor.organization_id, {k: v for k, v in body.items() if k != 'version'})


def _resolve_and_validate(db, actor, entity, op, plan, shown, facts, zone, before=None, row=None):
    """Draft, resolve names/addresses and validate; allow one bounded repair pass on validation/resolution errors."""
    repair = None
    for attempt in range(2):
        drafted = drafting.draft(plan.request, shown, schema(op), facts, repair=repair)
        if row is not None:
            drafted = workspace.strip_secrets({k: v for k, v in drafted.items() if k != 'version'})
        elif entity in records.CATALOG:
            drafted['kind'] = entity
        try:
            body = drafting.resolve(db, actor.organization_id, drafted, zone, before, free_text_address=entity == 'SETTINGS')
            payload = {**body, 'version': row.version} if row is not None else body
            return drafting.validate(op.body, payload)
        except drafting.Incomplete as error:
            if attempt == 1: raise
            repair = error.problems


def update(db, actor, entity, name, op, plan, row, subject):
    if entity == 'ORDER':
        if row.status in {'COMPLETED', 'CANCELLED'}:
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}, so it can’t be edited.', reference(entity, row))
        if row.status == 'ASSIGNED' and row.route_id and db is not None:
            route = db.scalar(select(Route).where(Route.organization_id == actor.organization_id, Route.id == row.route_id))
            if route and route.status == 'PLANNED' and route.locked:
                return reply(f'Order {row.number}’s route is locked. Unlock or unassign it before editing.', reference(entity, row))
    before = records.editable(db, actor, entity, row)
    shown = drafting.humanize(db, actor.organization_id, before)
    zone, facts = context(db, actor)
    body = _resolve_and_validate(db, actor, entity, op, plan, shown, facts, zone, before=before, row=row)
    lines = drafting.lines(normalized(db, actor, op, {**before, 'version': row.version}), normalized(db, actor, op, body))
    if not lines: return reply(f'{subject[:1].upper()}{subject[1:]} already looks like that. Nothing to change.', reference(entity, row))
    return proposal(name, op, subject, body, lines, None if op.whole else row.id, reference(entity, row))


def create(db, actor, entity, name, op, plan):
    zone, facts = context(db, actor)
    body = _resolve_and_validate(db, actor, entity, op, plan, {}, facts, zone)
    data = body.get('data', body)
    subject = data.get('company_name') or data.get('name') or body.get('code') or f'a new {records.NOUNS[entity]}'
    lines = drafting.lines(None, normalized(db, actor, op, body))
    if entity == 'QUOTE':
        # A quote is for a prospect: priced by a chosen rate card, never for an existing shipper.
        if body.get('shipper_id') or not body.get('rate_card_id'):
            return reply('Which rate card should the quote use? Quotes are for new customers; existing shippers get orders.')
        lines.insert(0, price_line(db, actor, op, body))
    return proposal(name, op, subject, body, lines)


def price_line(db, actor, op, body):
    try: price = orders.price_or_review(db, actor, orders.authorized_booking(actor, op.body.model_validate(body), prospect=True))
    except HTTPException as error: return {'field': 'Price', 'after': f'Not priced: {error.detail}'}
    total = price.get('total')
    return {'field': 'Price', 'after': f'{price.get("currency", "")} {total}'.strip() if total is not None
        else f'Needs price review: {price.get("review_reason") or "not priced"}'}


def new_order(db, actor, plan, op):
    """New Orders use the Order agent's booking extraction, the same path as email intake."""
    from app.intake import booking, extract, geocode
    from app.intake.service import booking_context
    term = plan.target.strip()
    if not term: return reply('Which shipper is this order for?')
    shippers = records.find(db, actor.organization_id, 'SHIPPER', term)
    if not shippers: return reply(f'I couldn’t find a shipper matching “{term}”.')
    if len(shippers) > 1:
        return reply(f'More than one shipper matches “{term}”: {", ".join(records.label("SHIPPER", s) for s in shippers)}. Which one?')
    shipper = shippers[0]
    ctx = booking_context(db, actor.organization_id, shipper)
    hint = {'name': shipper.company_name or shipper.name, 'warehouse_city': (shipper.warehouse or {}).get('city')}
    # Like the manual New Order form, a dispatcher who names no service level gets the company default.
    default = next((s['name'] for s in ctx['services'] if s['id'] == ctx['default_service_id']), None)
    body = plan.request + (f'\nIf no service level is stated, use {default}.' if default else '')
    try: result = extract.extract({'subject': 'Dispatcher chat booking', 'body': body, 'received_at': now().isoformat()}, {**ctx, 'shipper': hint})
    except extract.Unavailable: return reply(UNAVAILABLE)
    if isinstance(result, extract.ExtractionBatch): return reply('That sounds like more than one order. Let’s create them one at a time.')
    if result.review_reasons: raise drafting.Incomplete(result.review_reasons)
    if not result.is_order_request: return reply('Tell me the pickup, drop-off, time and items for the new order.')
    draft, missing = booking.build(result, ctx, geocode.verify)
    if missing: raise drafting.Incomplete(missing)
    body = drafting.validate(op.body, draft)
    lines = drafting.lines(None, normalized(db, actor, op, body))
    lines.insert(0, price_line(db, actor, op, body))
    return proposal('ORDER.CREATE', op, f'a new order for {records.label("SHIPPER", shipper)}', body, lines, references=reference('SHIPPER', shipper))


def close(db, actor, entity, name, op, row):
    """Cancel, complete, archive or delete: the body is only the record version."""
    verb = name.split('.')[1]
    if entity == 'ORDER':
        refs = reference(entity, row)
        if verb == 'CANCEL' and row.status in {'CANCELLED', 'COMPLETED'}:
            return reply(f'Order {row.number} is already {STATUS[row.status].lower()}, so there is nothing to cancel.', refs)
        if verb == 'CANCEL' and row.status != 'NEW':
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Only unassigned orders can be cancelled; unassign it first.', refs)
        if verb == 'COMPLETE' and row.status not in {'ASSIGNED', 'IN_PROGRESS'}:
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Only assigned or on-the-road orders can be completed.', refs)
        if verb == 'RESTORE':
            return proposal(name, op, f'order {row.number}', {'version': row.version},
                [{'field': 'Orders list', 'before': 'Archived', 'after': f'Shown ({STATUS[row.status]})'}], row.id, refs)
        if verb == 'ARCHIVE':
            if row.status in {'ASSIGNED', 'IN_PROGRESS'}:
                return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Unassign or complete it first, then I can archive it.', refs)
            lines = [{'field': 'Status', 'before': STATUS[row.status], 'after': 'Cancelled'}] if row.status == 'NEW' else []
            lines.append({'field': 'Orders list', 'before': 'Shown', 'after': 'Archived (history kept)'})
            return proposal(name, op, f'order {row.number}', {'version': row.version}, lines, row.id, refs)
        after = 'Cancelled' if verb == 'CANCEL' else 'Completed (remaining stops closed without proof of delivery)'
        return proposal(name, op, f'order {row.number}', {'version': row.version}, [{'field': 'Status', 'before': STATUS[row.status], 'after': after}],
            row.id, refs)
    subject = records.label(entity, row)
    return proposal(name, op, subject, {'version': row.version},
        [{'field': records.NOUNS[entity].capitalize(), 'before': 'Active', 'after': 'Deleted' if op.verb == 'Delete' else 'Archived'}],
        row.id, reference(entity, row))


def unassign_driver_row(db, actor, name, op, driver):
    """Unassign a driver's planned unlocked route (all assigned orders on it)."""
    refs = reference('DRIVER', driver)
    route = db.scalar(select(Route).where(Route.organization_id == actor.organization_id, Route.driver_id == driver.id,
        Route.status.in_(['PLANNED', 'IN_PROGRESS'])).order_by(Route.updated_at.desc()).limit(1))
    if route is None:
        return reply(f'{driver.name} has no assigned orders right now.', refs)
    if route.status != 'PLANNED' or route.locked:
        return reply(f'{driver.name}’s route is locked or already underway, so it can’t be unassigned now.', refs)
    assigned = db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.route_id == route.id,
        Order.status == 'ASSIGNED', Order.archived_at.is_(None)).order_by(Order.number)).all()
    if not assigned:
        return reply(f'{driver.name} has no assigned orders right now.', refs)
    if len(assigned) == 1:
        order = assigned[0]
        lines = [{'field': f'Order {order.number}', 'before': driver.name, 'after': 'Unassigned'}]
        return proposal(name, op, f'order {order.number}', {'version': route.version, 'generation': route.generation},
            lines, route.id, reference('ORDER', order) + refs)
    lines = [{'field': f'Order {order.number}', 'before': driver.name, 'after': 'Unassigned'} for order in assigned[:10]]
    if len(assigned) > 10: lines.append({'field': 'More', 'after': f'{len(assigned) - 10} more orders'})
    order_refs = [ref for order in assigned[:5] for ref in reference('ORDER', order)]
    return proposal(name, op, f'{driver.name}’s {len(assigned)} assigned orders',
        {'version': route.version, 'generation': route.generation}, lines, route.id, order_refs + refs)


def unassign_request(db, actor, name, op, plan, context=None):
    """Unassign by order number OR by driver name (e.g. 'unassign order from Alex'), or from Monitor selection."""
    term = plan.target.strip() or plan.driver.strip()
    if plan.entity == 'DRIVER' and term:
        drivers = records.find(db, actor.organization_id, 'DRIVER', term)
        if not drivers: return reply(f'I couldn’t find a driver matching “{term}”.')
        if len(drivers) > 1:
            names = ', '.join(records.label('DRIVER', row) for row in drivers)
            return reply(f'More than one driver matches “{term}”: {names}. Which one do you mean?',
                [ref for row in drivers for ref in reference('DRIVER', row)])
        return unassign_driver_row(db, actor, name, op, drivers[0])
    if term:
        found_orders = records.find(db, actor.organization_id, 'ORDER', term)
        if len(found_orders) == 1: return unassign(db, actor, name, op, found_orders[0])
        if len(found_orders) > 1:
            names = ', '.join(records.label('ORDER', row) for row in found_orders)
            return reply(f'More than one order matches “{term}”: {names}. Which one do you mean?',
                [ref for row in found_orders for ref in reference('ORDER', row)])
        found_drivers = records.find(db, actor.organization_id, 'DRIVER', term)
        if len(found_drivers) == 1: return unassign_driver_row(db, actor, name, op, found_drivers[0])
        if len(found_drivers) > 1:
            names = ', '.join(records.label('DRIVER', row) for row in found_drivers)
            return reply(f'More than one driver matches “{term}”: {names}. Which one do you mean?',
                [ref for row in found_drivers for ref in reference('DRIVER', row)])
        return reply(f'I couldn’t find an order or driver matching “{term}”.')
    selected_order = _selected_row(db, actor.organization_id, 'ORDER', context)
    if selected_order is not None: return unassign(db, actor, name, op, selected_order)
    selected_driver = _selected_row(db, actor.organization_id, 'DRIVER', context)
    if selected_driver is not None: return unassign_driver_row(db, actor, name, op, selected_driver)
    if db is not None:
        assigned = db.scalars(select(Order).where(Order.organization_id == actor.organization_id, Order.status == 'ASSIGNED',
            Order.archived_at.is_(None)).limit(2)).all()
        if len(assigned) == 1: return unassign(db, actor, name, op, assigned[0])
    return reply('Which order or driver should I unassign? Tell me the order number or driver’s name.')


def unassign(db, actor, name, op, order):
    refs = reference('ORDER', order)
    if order.status != 'ASSIGNED' or not order.route_id:
        return reply(f'Order {order.number} is {STATUS[order.status].lower()}, so there is no driver to take it off.', refs)
    route = record(db, Route, actor, order.route_id)
    if route.status != 'PLANNED' or route.locked:
        return reply(f'Order {order.number}’s route is locked or already underway, so it can’t be unassigned now.', refs)
    driver = record(db, Driver, actor, route.driver_id)
    others = db.scalars(select(Order.number).where(Order.organization_id == actor.organization_id, Order.route_id == route.id,
        Order.id != order.id)).all()
    lines = [{'field': f'Order {order.number}', 'before': driver.name, 'after': 'Unassigned'}]
    if others: lines.append({'field': 'Also unassigned (same route)', 'after': ', '.join(others)})
    return proposal(name, op, f'order {order.number}', {'version': route.version, 'generation': route.generation}, lines, route.id, refs)


def resolve_issue(db, actor, name, op, plan, context=None):
    term = plan.target.strip()
    if not term:
        selected = _selected_row(db, actor.organization_id, 'ORDER', context)
        if selected is None: return reply('Which order’s issue should I resolve? Tell me the order number.')
        found = [selected]
    else:
        found = records.find(db, actor.organization_id, 'ORDER', term)
    if len(found) != 1:
        return reply(f'I couldn’t find an order matching “{term}”.' if not found else f'More than one order matches “{term}”. Which one?',
            [ref for row in found for ref in reference('ORDER', row)])
    order = found[0]
    issues = records.open_issues(db, actor.organization_id, order)
    if not issues: return reply(f'Order {order.number} has no open issues.', reference('ORDER', order))
    wanted = [i for i in issues if i.kind.replace('_', ' ').lower() in plan.request.lower()] if len(issues) > 1 else issues
    if len(wanted) != 1:
        kinds = ', '.join(i.kind.replace('_', ' ').lower() for i in issues)
        return reply(f'Order {order.number} has {len(issues)} open issues ({kinds}). Which one should I resolve?', reference('ORDER', order))
    issue = wanted[0]
    note = plan.request.strip()
    if len(note) < 3: return reply('What was done to resolve it? I’ll save that as the resolution note.', reference('ORDER', order))
    body = drafting.validate(op.body, {'version': issue.version, 'resolution': note[:2000]})
    lines = [{'field': issue.kind.replace('_', ' ').capitalize(), 'before': 'Open', 'after': 'Resolved'}, {'field': 'Resolution', 'after': body['resolution']}]
    return proposal(name, op, f'the issue on order {order.number}', body, lines, issue.id, reference('ORDER', order))


def confirm(db, actor, data, key):
    """Run a confirmed change through its workspace command as the signed-in dispatcher; every rule is checked again."""
    entity, op = records.named(data.operation)
    if op is None: raise HTTPException(422, 'This change can’t be made from chat.')
    verb = data.operation.split('.')[1]
    if (data.target_id is None) != (verb == 'CREATE' or op.whole): raise HTTPException(422, 'This change needs the record it applies to.')
    if any(data.body.get(secret) is not None for secret in workspace.SECRETS):
        raise HTTPException(422, 'Passwords, keys and secrets can’t be set from chat. Enter them in Integrations.')
    try: body = op.body.model_validate(data.body)
    except ValidationError as error: raise HTTPException(422, ' '.join(drafting.explain(error))) from None
    if verb == 'CREATE' and entity in records.CATALOG and body.kind != entity: raise HTTPException(422, 'This change can’t be made from chat.')
    password = None

    def run():
        nonlocal password
        result = op.run(db, actor, data.target_id, body, key)
        password = result.pop('initial_password', None) if isinstance(result, dict) else None
        identity = (result.get('id') or result.get('order_id')) if isinstance(result, dict) else None
        # Bulk changes have no single record; audit rows require one, so they name the confirming dispatcher.
        audit(db, actor, 'assistant.change_confirmed', identity or data.target_id or actor.id, actor.organization_id)
        label = (result.get('number') if entity == 'ORDER' else result.get('name') or (result.get('data') or {}).get('name')
            or result.get('number')) if isinstance(result, dict) else None
        message = done_many(entity, op, result['updated']) if isinstance(result, dict) and 'updated' in result else done(entity, verb, op, label)
        return {'message': message, 'id': str(identity) if identity else None,
            'version': result.get('version') if isinstance(result, dict) else None}
    # Only the public result is kept for retries; a new account's initial password is returned once and never stored.
    result = command(db, actor, key, 'assistant-change', data.model_dump(mode='json'), run)
    return {**result, 'initial_password': password}


def update_many(db, actor, op, plan, rows):
    """Draft one confirmed batch edit for multiple Orders (at most ORDER_BATCH_EDIT_LIMIT)."""
    if len(rows) > ORDER_BATCH_EDIT_LIMIT:
        return reply(f'I can edit up to {ORDER_BATCH_EDIT_LIMIT} orders at a time. Name the ones you want to change first.')
    zone, facts = context(db, actor)
    items, lines, refs = [], [], []
    for row in rows:
        if row.status in {'COMPLETED', 'CANCELLED'}:
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}, so it can’t be edited.', reference('ORDER', row))
        if row.status == 'ASSIGNED' and row.route_id and db is not None:
            route = db.scalar(select(Route).where(Route.organization_id == actor.organization_id, Route.id == row.route_id))
            if route and route.status == 'PLANNED' and route.locked:
                return reply(f'Order {row.number}’s route is locked. Unlock or unassign it before editing.', reference('ORDER', row))
        before = records.editable(db, actor, 'ORDER', row)
        shown = drafting.humanize(db, actor.organization_id, before)
        body = _resolve_and_validate(db, actor, 'ORDER', op, plan, shown, facts, zone, before=before, row=row)
        row_lines = drafting.lines(normalized(db, actor, op, {**before, 'version': row.version}), normalized(db, actor, op, body))
        if not row_lines: continue
        items.append({'id': str(row.id), 'body': body})
        refs += reference('ORDER', row)
        for line in row_lines[:4]:
            lines.append({'field': f'{row.number} {line["field"]}', 'before': line['before'], 'after': line['after']})
    if not items:
        return reply('Those orders already look like that. Nothing to change.', [ref for row in rows for ref in reference('ORDER', row)])
    if len(lines) > drafting.LINES:
        lines = lines[:drafting.LINES] + [{'field': 'More', 'before': None, 'after': f'{len(lines) - drafting.LINES} more changes'}]
    batch_name, batch_op = records.operation('ORDER', 'UPDATE_MANY')
    subject = f'{len(items)} order{"" if len(items) == 1 else "s"}'
    return proposal(batch_name, batch_op, subject, {'items': items}, lines, references=refs[:8])


def close_many(db, actor, verb, rows):
    """Cancel, archive, complete or restore multiple Orders in one confirmed batch."""
    if len(rows) > ORDER_BATCH_CLOSE_LIMIT:
        return reply(f'I can update up to {ORDER_BATCH_CLOSE_LIMIT} orders at a time. Narrow down which ones to include.')
    items, lines, refs = [], [], []
    for row in rows:
        check = close(db, actor, 'ORDER', f'ORDER.{verb}', records.OPERATIONS['ORDER'][verb], row)
        if not check['change']: return check
        items.append({'id': str(row.id), 'version': row.version})
        refs += reference('ORDER', row)
        summary = check['change'].lines[-1]
        lines.append({'field': f'Order {row.number}', 'before': summary.before, 'after': summary.after})
    if len(lines) > 10:
        lines = lines[:10] + [{'field': 'More', 'before': None, 'after': f'{len(lines) - 10} more orders'}]
    batch_name, batch_op = records.operation('ORDER', f'{verb}_MANY')
    return proposal(batch_name, batch_op, f'{len(items)} orders', {'items': items}, lines, references=refs[:8])


def run_order_batch_update(db, actor, body, key):
    for item in body.items: orders.update_order(db, actor, item.id, item.body, f'{key}:{item.id}')
    return {'updated': len(body.items)}


def _run_order_batch_close(db, actor, body, key, runner):
    for item in body.items: runner(db, actor, item.id, Version(version=item.version), f'{key}:{item.id}')
    return {'updated': len(body.items)}


def run_order_batch_cancel(db, actor, body, key): return _run_order_batch_close(db, actor, body, key, orders.cancel_order)
def run_order_batch_archive(db, actor, body, key): return _run_order_batch_close(db, actor, body, key, orders.archive_order)
def run_order_batch_complete(db, actor, body, key): return _run_order_batch_close(db, actor, body, key, orders.complete_order)
def run_order_batch_restore(db, actor, body, key): return _run_order_batch_close(db, actor, body, key, orders.restore_order)


def done(entity, verb, op, label):
    noun = records.NOUNS[entity]
    subject = f'{noun} {label}' if label else ('the company settings' if entity == 'SETTINGS' else f'the {noun}')
    return f'Done. {subject[:1].upper()}{subject[1:]} {"is" if entity != "SETTINGS" else "are"} {past(op)}.'


def past(op):
    return op.past or {'Create': 'created', 'Add': 'added', 'Edit': 'updated', 'Cancel': 'cancelled', 'Complete': 'completed',
        'Unassign': 'unassigned', 'Resolve': 'resolved', 'Archive': 'archived', 'Restore': 'restored', 'Delete': 'deleted', 'Mark read': 'marked read',
        'Discard': 'discarded', 'Send': 'sent', 'Sync': 'synced', 'Disconnect': 'disconnected', 'Clear': 'cleared'}[op.verb]


def done_many(entity, op, count):
    noun = records.NOUNS[entity] + ('' if count == 1 else 's')
    return f'Done. {count} {noun} {"is" if count == 1 else "are"} {past(op)}.'
