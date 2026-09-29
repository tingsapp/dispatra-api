from uuid import UUID
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import orders, dispatch, billing, issues, queries, mail
from .common import record, settings
from .models import Order, Route, Invoice, Quote, Issue, Shipper, Catalog, EmailDelivery
from .schemas import (Booking, OrderUpdate, OrderView, PriceView, QuoteView, Assignment,
    AssignmentView, RouteView, RouteCommand, Finalize, InvoiceView, Version, IssueInput, IssueView, IssueResolve, CatalogView, DeliveryProofView, BookingPreferences, BookingDriverView, QuoteSend, EmailDeliveryView)

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Manual orders and billing'])


def booking_actor(user: User = Depends(auth.company_user)):
    if user.role not in {'DISPATCHER','SHIPPER'}: raise HTTPException(403, 'Order booking access required.')
    return user


@router.get('/booking-options', response_model=list[CatalogView])
def options(slug: str, db: DB, user: User = Depends(booking_actor)):
    return db.scalars(select(Catalog).where(Catalog.organization_id == user.organization_id, Catalog.active.is_(True)).order_by(Catalog.kind,Catalog.code)).all()


@router.get('/booking-drivers', response_model=list[BookingDriverView])
def booking_drivers(slug: str, db: DB, user: User = Depends(booking_actor)):
    """Drivers a booking may request. Names only: no contact, location, duty or payout details."""
    return orders.bookable_drivers(db, user)


@router.post('/pricing/preview', response_model=PriceView)
def preview(slug: str, data: Booking, db: DB, user: User = Depends(booking_actor)):
    booking = orders.authorized_booking(user,data,prospect=user.role == 'DISPATCHER')
    result = orders.price_or_review(db,user,booking)
    if user.role == 'SHIPPER': result = {**result,'context':{}}
    return result


@router.post('/quotes', response_model=QuoteView, status_code=201)
def quote(slug: str, data: Booking, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return billing.create_quote(db,user,data,idempotency_key)


@router.get('/quotes/{identity}', response_model=QuoteView)
def get_quote(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    return record(db,Quote,user,identity)


@router.post('/quotes/{identity}/send', response_model=EmailDeliveryView, status_code=202)
def send_quote(slug: str, identity: UUID, data: QuoteSend, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return mail.send_quote(db, user, identity, data, idempotency_key)


@router.post('/orders', response_model=OrderView, status_code=201)
def create(slug: str, data: Booking, db: DB, idempotency_key: OperationKey, user: User = Depends(booking_actor)):
    return orders.create_order(db,user,data,idempotency_key)


@router.get('/orders', response_model=list[OrderView])
def list_orders(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=200),
    date_from: datetime | None = None, date_to: datetime | None = None,
    status: Literal['NEW','ASSIGNED','IN_PROGRESS','COMPLETED','INVOICED','CANCELLED'] | None = None,
    service_id: UUID | None = None,
    pricing_status: Literal['PRICED','NEEDS_ATTENTION'] | None = None,
    search: str | None = Query(default=None, max_length=120),
    user: User = Depends(booking_actor)):
    rows = queries.order_page(db, user, after=after, limit=limit, date_from=date_from,
        date_to=date_to, status=status, service_id=service_id,
        pricing_status=pricing_status, search=search)
    return [orders.order_view(row, user) for row in rows]


@router.get('/orders/{identity}', response_model=OrderView)
def get_order(slug: str, identity: UUID, db: DB, user: User = Depends(booking_actor)):
    return orders.order_view(orders.visible_order(db,user,identity),user)


@router.put('/orders/{identity}', response_model=OrderView)
def edit_order(slug: str, identity: UUID, data: OrderUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(booking_actor)):
    return orders.update_order(db,user,identity,data,idempotency_key)


@router.post('/orders/{identity}/cancel', response_model=OrderView)
def cancel(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(booking_actor)):
    return orders.cancel_order(db,user,identity,data,idempotency_key)


@router.post('/orders/{identity}/assign', response_model=AssignmentView)
def assign(slug: str, identity: UUID, data: Assignment, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return dispatch.assign(db,user,identity,data,idempotency_key)


@router.get('/routes', response_model=list[RouteView])
def routes(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=100), user: User = Depends(auth.dispatcher)):
    query = select(Route).where(Route.organization_id == user.organization_id).order_by(Route.id).limit(limit)
    if after: query = query.where(Route.id > after)
    return [dispatch.route_view(db,user,r) for r in db.scalars(query)]


@router.post('/routes/{identity}/release', response_model=RouteView)
def release(slug: str, identity: UUID, data: RouteCommand, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return dispatch.release(db,user,identity,data,idempotency_key)


@router.post('/orders/{identity}/invoice', response_model=InvoiceView, status_code=201)
def invoice(slug: str, identity: UUID, data: Finalize, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return billing.create_invoice(db,user,identity,data,idempotency_key)


@router.post('/invoices/{identity}/send', response_model=EmailDeliveryView, status_code=202)
def send_invoice(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return mail.send_invoice(db, user, identity, data, idempotency_key)


@router.get('/email-deliveries/{identity}', response_model=EmailDeliveryView)
def email_delivery(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    return record(db, EmailDelivery, user, identity)


@router.get('/invoices', response_model=list[InvoiceView])
def invoices(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=100), user: User = Depends(booking_actor)):
    query = select(Invoice).join(Order,Order.id == Invoice.order_id).where(Invoice.organization_id == user.organization_id).order_by(Invoice.id).limit(limit)
    if user.role == 'SHIPPER': query = query.where(Order.shipper_id == user.shipper_id, Order.billing_shipper_id == user.shipper_id)
    if after: query = query.where(Invoice.id > after)
    return [billing.invoice_view(row,user) for row in db.scalars(query)]


def visible_invoice(db,user,identity):
    row = record(db,Invoice,user,identity)
    order = orders.visible_order(db,user,row.order_id)
    if user.role == 'SHIPPER' and order.billing_shipper_id != user.shipper_id: raise HTTPException(404,'Invoice not found.')
    return row


@router.get('/invoices/{identity}', response_model=InvoiceView)
def get_invoice(slug: str, identity: UUID, db: DB, user: User = Depends(booking_actor)):
    return billing.invoice_view(visible_invoice(db,user,identity),user)


@router.get('/invoices/{identity}/document', response_class=HTMLResponse)
def document(slug: str, identity: UUID, db: DB, user: User = Depends(booking_actor)):
    row = visible_invoice(db,user,identity)
    return HTMLResponse(billing.invoice_document(row),headers={'Content-Disposition': f'attachment; filename="{row.number}.html"', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"})


@router.post('/orders/{identity}/issues', response_model=IssueView, status_code=201)
def report_issue(slug: str, identity: UUID, data: IssueInput, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.company_user)):
    return issues.report(db,user,identity,data,idempotency_key)


@router.get('/issues', response_model=list[IssueView])
def list_issues(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=100), user: User = Depends(auth.dispatcher)):
    query = select(Issue).where(Issue.organization_id == user.organization_id).order_by(Issue.id).limit(limit)
    if after: query = query.where(Issue.id > after)
    return db.scalars(query).all()


@router.post('/issues/{identity}/resolve', response_model=IssueView)
def resolve_issue(slug: str, identity: UUID, data: IssueResolve, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return issues.resolve(db,user,identity,data,idempotency_key)


@router.get('/orders/{identity}/delivery-proof', response_model=list[DeliveryProofView])
def delivery_proof(slug: str, identity: UUID, db: DB, user: User = Depends(booking_actor)):
    from .models import OrderStop, RouteStop, Evidence
    order=orders.visible_order(db,user,identity)
    visits=db.execute(select(RouteStop,OrderStop).join(OrderStop,OrderStop.id == RouteStop.stop_id).where(
        RouteStop.organization_id == user.organization_id,OrderStop.order_id == order.id,
        OrderStop.kind == 'DROPOFF',RouteStop.status == 'COMPLETED')).all()
    result=[]
    for visit,stop in visits:
        evidence=db.scalars(select(Evidence).where(Evidence.organization_id == user.organization_id,Evidence.stop_id == stop.id)).all()
        result.append({'stop_id':str(stop.id),'address':stop.data['address'],'completed_at':visit.completed_at,
            'recipient_name':visit.movements.get('recipient_name',''),'unattended':visit.movements.get('unattended',False),
            'evidence':[{'id':str(e.id),'kind':e.kind,'captured_at':e.captured_at} for e in evidence]})
    return result


@router.get('/quotes', response_model=list[QuoteView])
def list_quotes(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=200), user: User = Depends(auth.dispatcher)):
    query = select(Quote).where(Quote.organization_id == user.organization_id).order_by(Quote.id).limit(limit)
    if after: query = query.where(Quote.id > after)
    return db.scalars(query).all()


@router.get('/booking-preferences', response_model=BookingPreferences)
def booking_preferences(slug: str, db: DB, user: User = Depends(booking_actor)):
    _, config = settings(db, user)
    return BookingPreferences(**{field:getattr(config,field) for field in BookingPreferences.model_fields})
