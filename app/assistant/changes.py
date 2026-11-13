"""Chat record changes: the assistant drafts a change, the dispatcher confirms it, and the workspace command saves it.

A proposal's body is exactly that command's request, and confirmation runs the command as the signed-in dispatcher, so a
chat change can do nothing their own workspace session could not. The command re-checks company, version, state,
idempotency, audit and events; provider text never reaches a write without the dispatcher's confirmation."""
from datetime import datetime
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from app.models import now
from app.services import audit
from app.operations import orders
from app.operations.common import command, record, settings
from app.operations.models import Order, Route, Driver, Catalog, RateCard
from . import drafting, provider, records
from .actions import order_reference, driver_reference
from .schemas import AssistantChange, AssistantReference
from .search import company_zone

UNAVAILABLE = 'I can’t prepare changes right now. Try again in a moment, or make the change in the workspace.'
STATUS = {'NEW': 'Unassigned', 'ASSIGNED': 'Assigned', 'IN_PROGRESS': 'On the road', 'COMPLETED': 'Completed', 'CANCELLED': 'Cancelled'}


def reply(message, references=(), change=None):
    return {'message': message, 'references': list(references), 'action': None, 'change': change}


def ask(problems, noun):
    if len(problems) == 1: return reply(problems[0])
    return reply(f'I need a bit more before I can prepare this {noun}:\n' + '\n'.join(f'- {item}' for item in problems))


def proposal(name, op, subject, body, lines, target_id=None, references=()):
    entity, _, _ = name.partition('.')
    change = AssistantChange(operation=name, title=f'{op.verb} {records.NOUNS[entity]}', subject=subject, target_id=target_id,
        destructive=op.destructive, lines=lines, body=body)
    return reply(f'Here’s the change for {subject}. Nothing is saved until you confirm below.', references, change)


def reference(entity, row):
    if entity == 'ORDER': return [order_reference(row)]
    if entity == 'DRIVER': return [driver_reference(row)]
    if entity in {'SHIPPER', 'VEHICLE'}:
        return [AssistantReference(key=f'{entity}:{row.id}', kind=entity, id=str(row.id), label=records.label(entity, row), detail=row.number)]
    return []


def prepare(db, actor, plan):
    """Resolve the record the dispatcher named and draft the change without saving anything."""
    found = records.operation(plan.entity, plan.operation)
    if not found:
        return reply('I can’t make that change from chat. Logins, passwords, billing and integrations are handled in the workspace.')
    name, op = found
    entity, verb = name.split('.')
    noun = records.NOUNS[entity]
    try:
        if name == 'ORDER.CREATE': return new_order(db, actor, plan, op)
        if verb == 'CREATE': return create(db, actor, entity, name, op, plan)
        if entity == 'SETTINGS': return update(db, actor, entity, name, op, plan, settings(db, actor)[0], 'company settings')
        if entity == 'ISSUE': return resolve_issue(db, actor, name, op, plan)
        term = plan.target.strip()
        if not term: return reply(f'Which {noun}? Tell me its {"number" if entity == "ORDER" else "name"}.')
        rows = records.find(db, actor.organization_id, entity, term)
        if not rows: return reply(f'I couldn’t find a {noun} matching “{term}”.')
        if len(rows) > 1:
            names = ', '.join(records.label(entity, row) for row in rows)
            return reply(f'More than one {noun} matches “{term}”: {names}. Which one do you mean?',
                [ref for row in rows for ref in reference(entity, row)])
        row = rows[0]
        if verb == 'UPDATE': return update(db, actor, entity, name, op, plan, row, records.label(entity, row))
        if verb == 'UNASSIGN': return unassign(db, actor, name, op, row)
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
    shape = op.body.model_json_schema()
    shape.get('properties', {}).pop('version', None)
    shape['required'] = [field for field in shape.get('required', []) if field != 'version']
    return shape


def normalized(db, actor, op, body):
    """Body as the workspace schema would save it, with names for display; raw when legacy data no longer validates."""
    try: body = drafting.validate(op.body, body)
    except drafting.Incomplete: pass
    return drafting.humanize(db, actor.organization_id, {k: v for k, v in body.items() if k != 'version'})


def update(db, actor, entity, name, op, plan, row, subject):
    if entity == 'ORDER' and row.status != 'NEW':
        return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Only unassigned orders can be edited; unassign it first.', reference(entity, row))
    before = records.editable(db, actor, entity, row)
    shown = drafting.humanize(db, actor.organization_id, before)
    zone, facts = context(db, actor)
    drafted = drafting.draft(plan.request, shown, schema(op), facts)
    drafted.pop('version', None)
    body = drafting.resolve(db, actor.organization_id, drafted, zone, before, free_text_address=entity == 'SETTINGS')
    body = drafting.validate(op.body, {**body, 'version': row.version})
    lines = drafting.lines(normalized(db, actor, op, {**before, 'version': row.version}), normalized(db, actor, op, body))
    if not lines: return reply(f'{subject[:1].upper()}{subject[1:]} already looks like that. Nothing to change.', reference(entity, row))
    return proposal(name, op, subject, body, lines, None if entity == 'SETTINGS' else row.id, reference(entity, row))


def create(db, actor, entity, name, op, plan):
    zone, facts = context(db, actor)
    drafted = drafting.draft(plan.request, {}, schema(op), facts)
    if entity in records.CATALOG: drafted['kind'] = entity
    body = drafting.validate(op.body, drafting.resolve(db, actor.organization_id, drafted, zone))
    data = body.get('data', body)
    subject = data.get('company_name') or data.get('name') or body.get('code') or f'a new {records.NOUNS[entity]}'
    return proposal(name, op, subject, body, drafting.lines(None, normalized(db, actor, op, body)))


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
    try: result = extract.extract({'subject': 'Dispatcher chat booking', 'body': plan.request, 'received_at': now().isoformat()}, {**ctx, 'shipper': hint})
    except extract.Unavailable: return reply(UNAVAILABLE)
    if isinstance(result, extract.ExtractionBatch): return reply('That sounds like more than one order. Let’s create them one at a time.')
    if result.review_reasons: raise drafting.Incomplete(result.review_reasons)
    if not result.is_order_request: return reply('Tell me the pickup, drop-off, time and items for the new order.')
    draft, missing = booking.build(result, ctx, geocode.verify)
    if missing: raise drafting.Incomplete(missing)
    body = drafting.validate(op.body, draft)
    lines = drafting.lines(None, normalized(db, actor, op, body))
    try:
        price = orders.price_or_review(db, actor, orders.authorized_booking(actor, op.body.model_validate(body), prospect=True))
        lines.insert(0, {'field': 'Price', 'after': f'{price.get("currency", "")} {price["total"]}'.strip() if price.get('total') is not None
            else f'Needs price review: {price.get("review_reason") or "not priced"}'})
    except HTTPException: pass
    return proposal('ORDER.CREATE', op, f'a new order for {records.label("SHIPPER", shipper)}', body, lines, references=reference('SHIPPER', shipper))


def close(db, actor, entity, name, op, row):
    """Cancel, complete, archive or delete: the body is only the record version."""
    verb = name.split('.')[1]
    if entity == 'ORDER':
        refs = reference(entity, row)
        if verb == 'CANCEL' and row.status != 'NEW':
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Only unassigned orders can be cancelled; unassign it first.', refs)
        if verb == 'COMPLETE' and row.status not in {'ASSIGNED', 'IN_PROGRESS'}:
            return reply(f'Order {row.number} is {STATUS[row.status].lower()}. Only assigned or on-the-road orders can be completed.', refs)
        after = 'Cancelled' if verb == 'CANCEL' else 'Completed (remaining stops closed without proof of delivery)'
        return proposal(name, op, f'order {row.number}', {'version': row.version}, [{'field': 'Status', 'before': STATUS[row.status], 'after': after}],
            row.id, refs)
    subject = records.label(entity, row)
    return proposal(name, op, subject, {'version': row.version},
        [{'field': records.NOUNS[entity].capitalize(), 'before': 'Active', 'after': 'Deleted' if op.verb == 'Delete' else 'Archived'}],
        row.id, reference(entity, row))


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


def resolve_issue(db, actor, name, op, plan):
    term = plan.target.strip()
    if not term: return reply('Which order’s issue should I resolve? Tell me the order number.')
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
    if (data.target_id is None) != (verb == 'CREATE' or entity == 'SETTINGS'): raise HTTPException(422, 'This change needs the record it applies to.')
    try: body = op.body.model_validate(data.body)
    except ValidationError as error: raise HTTPException(422, ' '.join(drafting.explain(error))) from None
    if verb == 'CREATE' and entity in records.CATALOG and body.kind != entity: raise HTTPException(422, 'This change can’t be made from chat.')
    password = None

    def run():
        nonlocal password
        result = op.run(db, actor, data.target_id, body, key)
        password = result.pop('initial_password', None) if isinstance(result, dict) else None
        identity = (result.get('id') or result.get('order_id')) if isinstance(result, dict) else None
        audit(db, actor, 'assistant.change_confirmed', identity or data.target_id, actor.organization_id)
        label = result.get('number') or result.get('name') if isinstance(result, dict) else None
        return {'message': done(entity, verb, op, label), 'id': str(identity) if identity else None,
            'version': result.get('version') if isinstance(result, dict) else None}
    # Only the public result is kept for retries; a new account's initial password is returned once and never stored.
    result = command(db, actor, key, 'assistant-change', data.model_dump(mode='json'), run)
    return {**result, 'initial_password': password}


def done(entity, verb, op, label):
    noun = records.NOUNS[entity]
    subject = f'{noun} {label}' if label else ('the company settings' if entity == 'SETTINGS' else f'the {noun}')
    past = {'Create': 'created', 'Add': 'added', 'Edit': 'updated', 'Cancel': 'cancelled', 'Complete': 'completed',
        'Unassign': 'unassigned', 'Resolve': 'resolved', 'Archive': 'archived', 'Delete': 'deleted'}[op.verb]
    return f'Done. {subject[:1].upper()}{subject[1:]} {"is" if entity != "SETTINGS" else "are"} {past}.'
