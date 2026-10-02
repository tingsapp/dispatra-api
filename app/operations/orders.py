"""One Order creation path for dispatchers and authenticated Shippers."""
from contextlib import contextmanager
from uuid import UUID, uuid4
from fastapi import HTTPException
from app.database import context
from sqlalchemy import select, delete
from app.models import now
from app.services import audit
from .models import Order, OrderStop, OrderItem, Issue, Driver, Route, RouteStop
from .schemas import Booking, OrderView
from .common import record, command, version, changed, company_lock, operational_shipper
from .pricing import quote
from .identifiers import issue_number


def authorized_booking(actor, booking, prospect=False):
    if actor.role not in {'DISPATCHER', 'SHIPPER'}: raise HTTPException(403, 'Order access required.')
    if actor.role == 'SHIPPER':
        if booking.shipper_id and booking.shipper_id != actor.shipper_id: raise HTTPException(404, 'Shipper not found.')
        if booking.billing_shipper_id and booking.billing_shipper_id != actor.shipper_id: raise HTTPException(403, 'Billing account cannot be changed.')
        if booking.rate_card_id or booking.adjustments or booking.distance_km is not None or booking.imported_total is not None or booking.imported_tax is not None or booking.internal_notes:
            raise HTTPException(403, 'Commercial overrides require dispatcher access.')
        booking = booking.model_copy(update={'shipper_id': actor.shipper_id, 'billing_shipper_id': actor.shipper_id})
    if not prospect and booking.shipper_id is None: raise HTTPException(422, 'Select a Shipper for the Order.')
    return booking


@contextmanager
def company_scope(db, actor):
    """RLS hides drivers from Shipper sessions. Widen to company scope only for an explicit id/name projection, then restore."""
    context(db, actor.organization_id)
    try: yield
    finally: context(db, actor.organization_id, actor.role == 'ADMIN', actor.shipper_id, actor.driver_id)


def bookable_drivers(db, actor, identity=None):
    query = select(Driver.id, Driver.name).where(Driver.organization_id == actor.organization_id, Driver.active.is_(True), Driver.archived_at.is_(None))
    if identity: query = query.where(Driver.id == identity)
    with company_scope(db, actor): return [{'id': row.id, 'name': row.name} for row in db.execute(query.order_by(Driver.name))]


def check_preferred_driver(db, actor, booking, current=None):
    """A requested driver is a preference for dispatch, never an assignment; it must be a current active driver when chosen."""
    identity = booking.preferred_driver_id
    if identity is None or str(identity) == current: return
    if not bookable_drivers(db, actor, identity): raise HTTPException(422, 'Choose an available driver or no preference.')


def visible_order(db, actor, identity, lock=False):
    row = record(db, Order, actor, identity, lock)
    if actor.role == 'SHIPPER' and row.shipper_id != actor.shipper_id: raise HTTPException(404, 'Order not found.')
    if actor.role not in {'DISPATCHER', 'SHIPPER'}: raise HTTPException(403, 'Commercial Order access required.')
    return row


def order_view(row, actor):
    result = OrderView.model_validate(row).model_dump(mode='json')
    if actor.role == 'SHIPPER':
        result['facts']['internal_notes'] = ''
        result['pricing']['context'] = {}
        if row.billing_shipper_id != actor.shipper_id:
            result['booking'] = {'shipper': result['booking']['shipper']}
    return result


def booking_snapshot(db, actor, booking):
    def details(identity):
        profile = customer = operational_shipper(db, actor, identity)
        if customer.status != 'ACTIVE': raise HTTPException(409, 'Shipper account must be active to book.')
        return {'id': str(identity), 'name': customer.name, 'company_name': profile.company_name,
            'email': customer.email, 'phone': customer.phone, 'warehouse': profile.warehouse, 'terms': profile.terms}
    return {'shipper': details(booking.shipper_id), 'payer': details(booking.billing_shipper_id or booking.shipper_id)}


def price_or_review(db, actor, booking):
    try: return quote(db, actor, booking)
    except HTTPException as error:
        if error.status_code not in {409,503}: raise
        return {'status': 'NEEDS_ATTENTION', 'review_reason': error.detail, 'stage': 'ESTIMATE'}


def save_children(db, actor, row, booking):
    for stop in booking.stops:
        db.add(OrderStop(id=stop.id, organization_id=actor.organization_id, order_id=row.id,
            kind=stop.kind, data=stop.model_dump(mode='json')))
    db.flush()
    for item in booking.items:
        db.add(OrderItem(id=item.id, organization_id=actor.organization_id, order_id=row.id,
            pickup_id=item.pickup_id, delivery_id=item.delivery_id, quantity=item.quantity, data=item.model_dump(mode='json')))
    db.flush()


def create_order(db, actor, data, key):
    booking = authorized_booking(actor, data)
    def run():
        company_lock(db, actor)
        check_preferred_driver(db, actor, booking)
        identity = uuid4()
        row = Order(id=identity, organization_id=actor.organization_id, number=issue_number(db, actor, Order, 'O'),
            shipper_id=booking.shipper_id, billing_shipper_id=booking.billing_shipper_id or booking.shipper_id,
            service_id=booking.service_id, source='SHIPPER_PORTAL' if actor.role == 'SHIPPER' else 'DISPATCHER', status='NEW',
            scheduled_at=booking.scheduled_at, facts=booking.model_dump(mode='json'),
            booking=booking_snapshot(db, actor, booking), pricing=price_or_review(db, actor, booking))
        db.add(row); db.flush()
        save_children(db, actor, row, booking)
        audit(db, actor, 'order.created', row.id, actor.organization_id)
        return order_view(row, actor)
    return command(db, actor, key, 'order-create', booking.model_dump(mode='json'), run)


def update_order(db, actor, identity, data, key):
    booking = authorized_booking(actor, data.booking)
    def run():
        company_lock(db, actor)
        row = visible_order(db, actor, identity, True); version(row, data.version)
        if row.status != 'NEW': raise HTTPException(409, 'Only unassigned Orders can be edited. Record an issue for active work.')
        check_preferred_driver(db, actor, booking, row.facts.get('preferred_driver_id'))
        from .billing import retain_price
        retain_price(db, row)
        row.facts, row.booking = booking.model_dump(mode='json'), booking_snapshot(db, actor, booking)
        row.pricing = price_or_review(db, actor, booking)
        row.shipper_id, row.billing_shipper_id = booking.shipper_id, booking.billing_shipper_id or booking.shipper_id
        row.service_id = booking.service_id
        row.scheduled_at = booking.scheduled_at
        db.execute(delete(OrderItem).where(OrderItem.order_id == row.id, OrderItem.organization_id == actor.organization_id))
        db.execute(delete(OrderStop).where(OrderStop.order_id == row.id, OrderStop.organization_id == actor.organization_id))
        save_children(db, actor, row, booking)
        changed(db, actor, row, 'order.revised')
        return order_view(row, actor)
    return command(db, actor, key, 'order-edit:' + str(identity), data.model_dump(mode='json'), run)


def cancel_order(db, actor, identity, data, key):
    def run():
        company_lock(db, actor)
        row = visible_order(db, actor, identity, True); version(row, data.version)
        if row.status != 'NEW': raise HTTPException(409, 'Only an unassigned Order can be cancelled; release planned assignment first.')
        row.status = 'CANCELLED'; changed(db, actor, row, 'order.cancelled')
        return order_view(row, actor)
    return command(db, actor, key, 'order-cancel:' + str(identity), data.model_dump(), run)


def complete_order(db, actor, identity, data, key):
    """Dispatcher override: close an assigned Order's remaining visits without driver POD."""
    def run():
        company_lock(db, actor)
        row = visible_order(db, actor, identity, True); version(row, data.version)
        if actor.role != 'DISPATCHER': raise HTTPException(403, 'Dispatcher access required.')
        if row.status not in {'ASSIGNED', 'IN_PROGRESS'} or not row.route_id: raise HTTPException(409, 'Only an assigned or in-progress Order can be completed.')
        route = record(db, Route, actor, row.route_id, True)
        completed_at = now()
        visits = list(db.scalars(select(RouteStop).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(RouteStop.organization_id == actor.organization_id,
            RouteStop.route_id == route.id, OrderStop.order_id == row.id, RouteStop.status != 'COMPLETED').with_for_update(of=RouteStop)))
        for visit in visits:
            visit.status, visit.completed_at = 'COMPLETED', completed_at
            changed(db, actor, visit, 'stop.completed_by_dispatcher')
        row.status, row.completed_at = 'COMPLETED', completed_at
        from .billing import freeze_payout
        freeze_payout(db, actor, row)
        changed(db, actor, row, 'order.completed_by_dispatcher')
        remaining = db.scalar(select(Order.id).where(Order.organization_id == actor.organization_id, Order.route_id == route.id,
            Order.status.in_(['NEW', 'ASSIGNED', 'IN_PROGRESS'])))
        if not remaining and route.status in {'PLANNED', 'IN_PROGRESS'}:
            route.status, route.completed_at = 'COMPLETED', completed_at
            changed(db, actor, route, 'route.completed')
        else: changed(db, actor, route, 'route.progress')
        return order_view(row, actor)
    return command(db, actor, key, 'order-complete:' + str(identity), data.model_dump(), run)
