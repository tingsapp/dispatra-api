"""Chat assignments: the assistant prepares a checked proposal, the dispatcher confirms it, and the shared assignment
command re-validates and saves it. Provider text only selects literal search terms; it never reaches a write."""
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, func, or_
from app.models import now
from app.services import audit
from app.operations import dispatch
from app.operations.common import command, record
from app.operations.models import Order, Driver, Vehicle, Route
from app.operations.schemas import RouteCommand
from .schemas import AssistantAction, AssistantReference
from .search import pattern

CHOICES = 5
# Assignment rule failures in plain words; anything else is shown as the rule states it.
FRIENDLY = {
    'Driver must be On Duty before assignment.': '{driver} is off duty. They need to start their shift in the driver app first.',
    'No vehicle is attached to this driver.': '{driver} has no vehicle attached. Attach one on their driver profile first.',
    'Select a vehicle parking address with map coordinates or provide fresh driver GPS before planning.':
        'I can’t tell where {driver} is. There’s no recent GPS from their phone and their vehicle has no parking address on the map.',
    'Order needs pricing review before assignment.': 'This order needs its price reviewed before it can be assigned.',
    'Order quote has expired; reprice it before assignment.': 'This order’s price quote has expired. Reprice it first.',
    'Driver already has a locked or in-progress Route; finish it before assigning another Order.':
        '{driver}’s current route is locked or already underway, so I can’t add to it.',
}


def friendly(reason, driver):
    return FRIENDLY[reason].format(driver=driver.name) if reason in FRIENDLY else reason


def vehicle_name(vehicle):
    name = vehicle.data.get('name') or vehicle.number
    return f'{name} ({vehicle.plate})' if vehicle.plate and vehicle.plate != name else name


def order_reference(order):
    return AssistantReference(key=f'ORDER:{order.id}', kind='ORDER', id=str(order.id), label=order.number, order_number=order.number,
        detail=f'{order.status.replace("_", " ").capitalize()} · {order.booking.get("shipper", {}).get("name", "")}')


def driver_reference(driver):
    return AssistantReference(key=f'DRIVER:{driver.id}', kind='DRIVER', id=str(driver.id), label=driver.name,
        driver_id=str(driver.id), detail=driver.number)


def matches(db, org, model, fields, term):
    """Exact (case-insensitive) matches first; otherwise up to CHOICES partial matches."""
    base = select(model).where(model.organization_id == org)
    if model is Driver: base = base.where(Driver.archived_at.is_(None), Driver.active.is_(True))
    exact = db.scalars(base.where(or_(*[func.lower(f) == term.lower() for f in fields])).limit(CHOICES)).all()
    if exact: return exact
    return db.scalars(base.where(or_(*[f.ilike(pattern(term), escape='\\') for f in fields]))
        .order_by(model.updated_at.desc(), model.id).limit(CHOICES)).all()


def reply(message, references=(), action=None):
    return {'message': message, 'references': list(references), 'action': action}


def driver_on(db, org, order):
    route = db.scalar(select(Route).where(Route.organization_id == org, Route.id == order.route_id)) if order.route_id else None
    return route, (record(db, Driver, SimpleNamespace(organization_id=org), route.driver_id) if route else None)


def movable(db, org, order, route):
    """Why an assigned order cannot move to another driver, or None. Moving releases its Route, so it must hold only this order."""
    if route is None or route.status != 'PLANNED' or route.locked:
        return 'Its route is locked or already underway, so the driver can’t be changed now.'
    others = db.scalars(select(Order.number).where(Order.organization_id == org, Order.route_id == route.id, Order.id != order.id)).all()
    if others:
        return (f'It shares a route with {", ".join(others[:3])}{" and more" if len(others) > 3 else ""}. '
            'Change it from the order details so those orders are handled too.')
    return None


def prepare(db, actor, plan):
    """Resolve the order and driver the dispatcher named and check the assignment without saving anything."""
    from app.dispatch.candidates import blocker, evaluate
    org = actor.organization_id
    order_term, driver_term = plan.order_number.strip(), plan.driver.strip()
    if not order_term: return reply('Which order should I assign? Tell me its order number.')
    if not driver_term: return reply(f'Who should take order {order_term}? Tell me the driver’s name.')
    orders = matches(db, org, Order, [Order.number], order_term)
    if not orders: return reply(f'I couldn’t find an order matching “{order_term}”. Check the number and try again.')
    if len(orders) > 1:
        return reply(f'More than one order matches “{order_term}”. Which one do you mean?', map(order_reference, orders))
    order = orders[0]
    drivers = matches(db, org, Driver, [Driver.name, Driver.number], driver_term)
    if not drivers: return reply(f'I couldn’t find an active driver matching “{driver_term}”.')
    if len(drivers) > 1:
        names = ', '.join(d.name for d in drivers[:-1]) + f' or {drivers[-1].name}'
        return reply(f'I found more than one driver matching “{driver_term}”: {names}. Which one do you mean?', map(driver_reference, drivers))
    driver = drivers[0]
    route, current = driver_on(db, org, order)
    refs = [order_reference(order), driver_reference(driver)]
    if order.status == 'ASSIGNED' and current and current.id == driver.id:
        return reply(f'Order {order.number} is already assigned to {driver.name}. Nothing to change.', refs)
    if order.status == 'IN_PROGRESS':
        return reply(f'Order {order.number} is already on the road with {current.name if current else "a driver"}, so its driver can’t be changed now.', refs)
    if order.status in {'COMPLETED', 'CANCELLED'}:
        return reply(f'Order {order.number} is {order.status.lower()}, so it can’t be assigned.', refs)
    if order.status == 'ASSIGNED':
        reason = movable(db, org, order, route)
        if reason: return reply(f'Order {order.number} is assigned to {current.name if current else "another driver"}. {reason}', refs)
    # Moving an assigned order makes it unassigned first, so the unassigned-order rules apply to both cases.
    reason = blocker(SimpleNamespace(status='NEW', pricing=order.pricing))
    if reason: return reply(f'I can’t assign order {order.number} yet. {friendly(reason, driver)}', refs)
    candidates, excluded = evaluate(db, actor, order, road=False, driver_id=driver.id)
    if not candidates:
        reason = excluded[0]['reason'] if excluded else 'They are not available for this order.'
        return reply(f'{driver.name} can’t take order {order.number} right now. {friendly(reason, driver)}', refs)
    vehicle = record(db, Vehicle, actor, candidates[0]['vehicle_id'])
    action = AssistantAction(kind='ASSIGN', order_id=order.id, order_version=order.version, order_number=order.number,
        driver_id=driver.id, driver_name=driver.name, vehicle_name=vehicle_name(vehicle), current_driver_name=current.name if current else None)
    if current:
        message = f'Order {order.number} is with {current.name} now. I can move it to {driver.name} ({action.vehicle_name}). Confirm below and I’ll make the change.'
    else:
        message = f'{driver.name} can take order {order.number} in {action.vehicle_name}. Confirm below and I’ll assign it.'
    return reply(message, refs, action)


def confirm(db, actor, data, key):
    """Run a confirmed chat assignment through the shared release/assign commands; every hard rule is checked again."""
    from app.dispatch.service import assignment
    from app.dispatch.candidates import planned_start

    def run():
        order = record(db, Order, actor, data.order_id, True)
        if order.version != data.order_version:
            raise HTTPException(409, f'Order {order.number} changed since I checked it. Ask me again so I can recheck.')
        driver = record(db, Driver, actor, data.driver_id)
        if driver.archived_at or not driver.active: raise HTTPException(409, f'{driver.name} is no longer an active driver.')
        if not driver.vehicle_id: raise HTTPException(409, friendly('No vehicle is attached to this driver.', driver))
        route, current = driver_on(db, actor.organization_id, order)
        if order.status == 'ASSIGNED':
            if current and current.id == driver.id: raise HTTPException(409, f'Order {order.number} is already assigned to {driver.name}.')
            reason = movable(db, actor.organization_id, order, route)
            if reason: raise HTTPException(409, reason)
            dispatch.release(db, actor, route.id, RouteCommand(version=route.version, generation=route.generation), f'chat-release-{route.id.hex}-{route.version}')
        elif order.status != 'NEW':
            raise HTTPException(409, f'Order {order.number} can’t be assigned in its current state.')
        vehicle = record(db, Vehicle, actor, driver.vehicle_id)
        # `assignment` keeps the time of a Route the driver already has; this start applies only to a new Route.
        body = assignment(db, actor, order, {'driver_id': str(driver.id), 'vehicle_id': str(vehicle.id),
            'planned_at': planned_start(order, driver, None).isoformat()})
        try:
            result = dispatch.assign(db, actor, order.id, body, f'chat-assign-{order.id.hex}-{order.version}')
        except HTTPException as error:
            raise HTTPException(error.status_code, friendly(str(error.detail), driver)) from None
        audit(db, actor, 'assistant.assignment_confirmed', order.id, actor.organization_id)
        moved = f' (moved from {current.name})' if current else ''
        return {'message': f'Done. Order {order.number} is now assigned to {driver.name}{moved}. Their route has been updated.',
            'order_id': result['order_id'], 'order_version': result['order_version']}
    return command(db, actor, key, 'assistant-assign', data.model_dump(mode='json'), run)
