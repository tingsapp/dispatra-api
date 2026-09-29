"""Tenant-scoped read queries for the existing dispatcher and Shipper screens.

Commands live in domain services; projections and filters stay here so routers do not
reimplement business reads when another client or an agent uses the same API.
"""
from datetime import datetime
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from .models import Driver, Order, OrderStop, Route


def order_page(db, actor, *, after: UUID | None, limit: int,
               date_from: datetime | None, date_to: datetime | None,
               status: str | None, service_id: UUID | None,
               pricing_status: str | None, search: str | None):
    if (date_from and date_from.tzinfo is None) or (date_to and date_to.tzinfo is None):
        raise HTTPException(422, 'Date filters must include a time zone.')
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(422, 'Invalid date range.')
    query = select(Order).where(Order.organization_id == actor.organization_id)
    if actor.role == 'SHIPPER':
        query = query.where(Order.shipper_id == actor.shipper_id)
    if after:
        query = query.where(Order.id > after)
    if date_from:
        query = query.where(Order.scheduled_at >= date_from)
    if date_to:
        query = query.where(Order.scheduled_at < date_to)
    if status:
        query = query.where(Order.status == status)
    if service_id:
        query = query.where(Order.service_id == service_id)
    if pricing_status:
        query = query.where(Order.pricing['status'].astext == pricing_status)
    if search and search.strip():
        # Escape SQL wildcards: a search field is literal text, not a query language.
        term = search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = f'%{term}%'
        addresses = exists(select(OrderStop.id).where(
            OrderStop.organization_id == actor.organization_id,
            OrderStop.order_id == Order.id,
            OrderStop.data['address']['text'].astext.ilike(pattern, escape='\\')))
        fields = [Order.number.ilike(pattern, escape='\\'),
                  Order.booking['shipper']['name'].astext.ilike(pattern, escape='\\'),
                  Order.facts['external_reference'].astext.ilike(pattern, escape='\\'),
                  addresses]
        if actor.role == 'DISPATCHER':
            driver = exists(select(Route.id).join(Driver,
                (Driver.id == Route.driver_id) & (Driver.organization_id == Route.organization_id)).where(
                Route.id == Order.route_id, Route.organization_id == actor.organization_id,
                Driver.name.ilike(pattern, escape='\\')))
            fields.append(driver)
        query = query.where(or_(*fields))
    return db.scalars(query.order_by(Order.id).limit(limit)).all()
