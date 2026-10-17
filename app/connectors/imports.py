"""Hidden mapping into existing domain use cases. No provider schema is exposed in a user form."""
import hashlib
from decimal import Decimal
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import context, engine
from app.integrations import fleet, order_intake
from app.integrations.mapping import fingerprint, get, text
from app.models import now
from app.operations import directory, orders
from app.operations.models import Order, Shipper
from app.operations.schemas import Booking, OrderUpdate, ShipperData, Version
from .models import Connection, Inbox, Link

class Stale(Exception):pass

def key(connection,kind,external,revision):
    return hashlib.sha256(f'{connection}:{kind}:{external}:{revision}'.encode()).hexdigest()

def current(db,org,identity,lease):
    row=db.scalar(select(Connection).where(Connection.organization_id==org,Connection.id==identity).with_for_update())
    from app.models import Organization
    from .availability import enabled
    if row is None or row.lease!=lease or row.status=='DISCONNECTED' or row.paused or not enabled(db,row.provider,lock=True) or not db.scalar(select(Organization.id).where(Organization.id==org,Organization.active)):raise Stale()
    return row

def linked(db,org,connection,kind,external):
    return db.scalar(select(Link).where(Link.organization_id==org,Link.connection_id==connection,Link.kind==kind,Link.external_id==external))

def setup(db,org,profile):
    ctx=order_intake._context(db,org)
    ctx['map']=profile.maps.get('order',{})
    ctx['weight']=Decimal('0.45359237') if profile.weight_unit=='lb' else Decimal('1')
    ctx['size']={'in':Decimal('2.54'),'cm':Decimal('1'),'m':Decimal('100')}[profile.dimension_unit]
    # Vehicle defaults are already in Dispatra's storage units.
    from app.operations.models import Catalog
    ctx['types']=[{'id':str(t.id),'code':t.code,'name':t.data.get('name',t.code),'data':t.data} for t in db.scalars(select(Catalog).where(
        Catalog.organization_id==org,Catalog.kind=='VEHICLE_TYPE',Catalog.active.is_(True)))]
    from app.operations.common import settings
    _,company=settings(db,fleet.actor(org))
    if company.currency!=profile.currency:raise HTTPException(409,'CURRENCY_MISMATCH')
    return ctx

def _save_link(db,org,connection,kind,external,identity,revision):
    link=linked(db,org,connection,kind,external)
    if link is None:
        link=Link(organization_id=org,connection_id=connection,kind=kind,external_id=external,record_id=identity);db.add(link)
    link.fingerprint=revision

def fleet_record(org,identity,lease,profile,kind,record,ctx):
    mapping=profile.maps[kind]
    if kind=='vehicle':external,data=fleet.vehicle_data(record,mapping,ctx);unit=None
    else:external,data,unit=fleet.driver_data(record,mapping)
    digest=fingerprint({'data':data,'unit':unit,'profile':profile.revision})
    if kind=='driver':
        from app.intake import geocode
        located=geocode.verify(data['address']['text'],data['address']['postal_code'])
        if located:data={**data,'address':{**data['address'],'latitude':located['latitude'],'longitude':located['longitude']}}
    with Session(engine) as db,db.begin():
        context(db,org);current(db,org,identity,lease)
        link=linked(db,org,identity,kind.upper(),external)
        if link and link.fingerprint==digest:return False
        writer=fleet._write_vehicle(org,data) if kind=='vehicle' else fleet._write_driver(org,data,unit)
        record_id,_=writer(db,link.record_id if link else None,key(identity,kind,external,digest))
        _save_link(db,org,identity,kind.upper(),external,record_id,digest)
    return True

def _shipper(db,org,connection,found,operation):
    link=linked(db,org,connection,'SHIPPER',found['id'])
    row=db.get(Shipper,link.record_id) if link else db.scalar(select(Shipper).where(Shipper.organization_id==org,
        func.lower(Shipper.email)==found['email'],Shipper.warehouse.is_not(None),Shipper.archived_at.is_(None)))
    if row is None:
        if not found['address']:raise HTTPException(422,'The TMS did not supply a usable shipper address.')
        result=directory.save_shipper(db,fleet.actor(org),ShipperData(name=found['name'],kind='BUSINESS',company_name=found['company_name'] or found['name'],
            email=found['email'],phone=found['phone'],warehouse=found['address']),operation+'shipper')
        row=db.get(Shipper,UUID(str(result['id'])))
    if row.status!='ACTIVE':raise HTTPException(409,'Shipper account is on hold or inactive.')
    _save_link(db,org,connection,'SHIPPER',found['id'],row.id,None)
    return row

def order_record(org,identity,lease,profile,record,ctx):
    external=text(get(record,profile.maps['order'],'id'),200)
    if not external:raise fleet.Skip('The TMS order has no reference.')
    revision=fingerprint({'profile':profile.revision,'record':record})
    with Session(engine) as db,db.begin():
        context(db,org);current(db,org,identity,lease)
        inbox=db.scalar(select(Inbox).where(Inbox.organization_id==org,Inbox.connection_id==identity,Inbox.external_id==external,Inbox.kind=='ORDER',Inbox.revision==revision))
        if inbox and inbox.status in ['IMPORTED','IGNORED']:return 'unchanged'
        if inbox:inbox.status='RECEIVED'
        if inbox is None:
            inbox=Inbox(organization_id=org,connection_id=identity,external_id=external,revision=revision,
                profile_revision=profile.revision,kind='ORDER',payload=record,status='RECEIVED',reasons=[]);db.add(inbox);db.flush()
        inbox_id=inbox.id
    # Geocoding and mapping are outside the write transaction. Stable stop IDs also include the connection.
    mapped=order_intake.build(record,{**ctx,'organization_id':f'{org}:{identity}'})
    with Session(engine) as db,db.begin():
        context(db,org);current(db,org,identity,lease)
        inbox=db.get(Inbox,inbox_id,with_for_update=True)
        if inbox.status!='RECEIVED':return 'unchanged'
        link=linked(db,org,identity,'ORDER',external);order=db.get(Order,link.record_id) if link else None
        operation=key(identity,'ORDER',external,revision)
        try:
            with db.begin_nested():
                if mapped['missing']:raise fleet.Skip('The TMS order needs information before it can be imported.')
                if order and order.status!='NEW':raise fleet.Skip('An assigned, started or completed order cannot be changed by this connector.')
                if mapped['cancelled']:
                    if order:orders.cancel_order(db,fleet.actor(org),order.id,Version(version=order.version),operation)
                    inbox.status='IGNORED'
                else:
                    shipper=_shipper(db,org,identity,mapped['shipper'],operation)
                    booking=Booking.model_validate({**mapped['booking'],'shipper_id':str(shipper.id)})
                    if order:
                        orders.update_order(db,fleet.actor(org),order.id,OrderUpdate(version=order.version,booking=booking),operation)
                        result_id=order.id
                    else:
                        result=orders.create_order(db,fleet.actor(org),booking,operation,source='IMPORT');result_id=UUID(str(result['id']))
                    _save_link(db,org,identity,'ORDER',external,result_id,revision)
                    inbox.order_id=result_id;inbox.status='IMPORTED'
        except (fleet.Skip,HTTPException,ValidationError):
            inbox.status='NEEDS_REVIEW'
            # Only controlled, actionable missing-field descriptions; provider bodies/errors are never public.
            inbox.reasons=mapped['missing'][:30] or ['This order needs review before it can be imported.']
        if inbox.status in ['IMPORTED','IGNORED']:
            for previous in db.scalars(select(Inbox).where(Inbox.organization_id==org,Inbox.connection_id==identity,Inbox.external_id==external,Inbox.id!=inbox.id,Inbox.kind=='ORDER',Inbox.status=='NEEDS_REVIEW')):
                previous.status='IGNORED'
        return 'review' if inbox.status=='NEEDS_REVIEW' else 'imported' if inbox.status=='IMPORTED' else 'unchanged'


def review_record(org,identity,lease,profile,kind,record):
    external=text(get(record,profile.maps[kind],'id'),200) or 'Missing reference'
    revision=fingerprint({'kind':kind,'profile':profile.revision,'record':record})
    with Session(engine) as db,db.begin():
        context(db,org);current(db,org,identity,lease)
        row=db.scalar(select(Inbox).where(Inbox.organization_id==org,Inbox.connection_id==identity,Inbox.external_id==external,Inbox.kind==kind.upper(),Inbox.revision==revision))
        if row is None:
            db.add(Inbox(organization_id=org,connection_id=identity,external_id=external,revision=revision,kind=kind.upper(),
                profile_revision=profile.revision,payload=record,status='NEEDS_REVIEW',reasons=[f'This {kind} is missing information required by Dispatra.']))
