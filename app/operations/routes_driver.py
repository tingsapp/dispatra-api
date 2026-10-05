import base64
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import execution, dispatch
from .common import record, version, command, changed
from .models import Route, Driver, Evidence, OrderStop, Order, DutySession, Vehicle, Catalog
from .schemas import DutyStart, DutyEnd, DutyView, Telemetry, RouteCommand, StopCommand, EvidenceInput, RouteView, ProofEvidence

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Driver execution'])


from .schemas import DriverProfileView, DriverProfileUpdate, DriverOrderView

@router.get('/driver/profile', response_model=DriverProfileView)
def profile(slug: str, db: DB, user: User = Depends(auth.driver)):
    row = record(db,Driver,user,user.driver_id)
    duty = db.scalar(select(DutySession).where(DutySession.organization_id == user.organization_id, DutySession.driver_id == row.id, DutySession.ended_at.is_(None)))
    vehicle = record(db,Vehicle,user,row.vehicle_id) if row.vehicle_id else None
    return {'id': row.id, 'version': row.version, 'number': row.number, 'name': row.name, 'email': row.email, 'phone': row.phone,
        'address': row.address, 'service_city': row.service_city, 'vehicle_id': row.vehicle_id,
        'duty': DutyView.model_validate(duty).model_dump(mode='json') if duty else None,
        'location_permission': row.location_permission,
        'vehicle_name': ' · '.join(filter(None, [vehicle.data.get('name'), vehicle.data.get('plate')])) if vehicle else None}


@router.patch('/driver/profile', response_model=DriverProfileView)
def profile_update(slug: str, data: DriverProfileUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    def run():
        row = record(db,Driver,user,user.driver_id,True); version(row,data.version)
        row.phone = data.phone.strip()
        changed(db,user,row,'driver.profile_updated')
        return {'driver_id': str(row.id)}
    command(db,user,idempotency_key,'driver-profile',data.model_dump(mode='json'),run)
    return profile(slug,db,user)


@router.get('/driver/orders', response_model=list[DriverOrderView])
def driver_orders(slug: str, db: DB, user: User = Depends(auth.driver)):
    rows = db.execute(select(Order,Route.status).join(Route,Route.id == Order.route_id).where(Order.organization_id == user.organization_id,
        Route.organization_id == user.organization_id, Route.driver_id == user.driver_id, Order.status != 'CANCELLED').order_by(Order.scheduled_at.desc())).all()
    services = {c.id: c.data.get('name','') for c in db.scalars(select(Catalog).where(Catalog.organization_id == user.organization_id, Catalog.id.in_({o.service_id for o,_ in rows})))} if rows else {}
    return [{'id': o.id, 'number': o.number, 'status': o.status, 'scheduled_at': o.scheduled_at, 'completed_at': o.completed_at,
        'route_id': o.route_id, 'route_status': route_status, 'service_name': services.get(o.service_id,''),
        'shipper': {k: o.booking['shipper'].get(k, '') for k in ('name','company_name','phone','email')},
        'stops': [{k: stop.get(k, default) for k, default in [('id',None),('kind',None),('address',None),('contact_name',''),('phone',''),('instructions',''),
            ('window_start',None),('window_end',None),('unattended_allowed',False),('photo_required',False)]} for stop in o.facts['stops']],
        'items': o.facts['items']} for o, route_status in rows]


@router.post('/driver/duty', response_model=DutyView, status_code=201)
def duty_start(slug: str, data: DutyStart, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.start_duty(db,user,data,idempotency_key)


@router.post('/driver/duty/{identity}/end', response_model=DutyView)
def duty_end(slug: str, identity: UUID, data: DutyEnd, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.end_duty(db,user,identity,data,idempotency_key)


@router.post('/driver/location', response_model=dict, status_code=201)
def location(slug: str, data: Telemetry, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.telemetry(db,user,data,idempotency_key)


@router.get('/driver/routes', response_model=list[RouteView])
def routes(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=100), user: User = Depends(auth.driver)):
    query = select(Route).where(Route.organization_id == user.organization_id, Route.driver_id == user.driver_id).order_by(Route.id).limit(limit)
    if after: query = query.where(Route.id > after)
    return [dispatch.route_view(db,user,r) for r in db.scalars(query)]


@router.get('/driver/routes/{identity}', response_model=RouteView)
def get_route(slug: str, identity: UUID, db: DB, user: User = Depends(auth.driver)):
    return dispatch.route_view(db,user,execution.own_route(db,user,identity))


@router.post('/driver/routes/{identity}/start', response_model=RouteView)
def start(slug: str, identity: UUID, data: RouteCommand, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.start_route(db,user,identity,data,idempotency_key)


@router.post('/driver/routes/{identity}/stops/{visit_id}/complete', response_model=RouteView)
def complete(slug: str, identity: UUID, visit_id: UUID, data: StopCommand, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.complete_stop(db,user,identity,visit_id,data,idempotency_key)


@router.post('/driver/routes/{identity}/finish', response_model=RouteView)
def finish(slug: str, identity: UUID, data: RouteCommand, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.finish_route(db,user,identity,data,idempotency_key)


@router.post('/driver/stops/{identity}/evidence', response_model=dict, status_code=201)
def evidence(slug: str, identity: UUID, data: EvidenceInput, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.upload_evidence(db,user,identity,data,idempotency_key)


@router.get('/driver/stops/{identity}/evidence', response_model=list[ProofEvidence])
def stop_evidence(slug: str, identity: UUID, db: DB, user: User = Depends(auth.driver)):
    stop = record(db, OrderStop, user, identity)
    order = record(db, Order, user, stop.order_id)
    if not order.route_id: raise HTTPException(404, 'Stop not found.')
    route = execution.own_route(db, user, order.route_id)
    if route.status != 'IN_PROGRESS': return []
    return db.scalars(select(Evidence).where(Evidence.organization_id == user.organization_id,
        Evidence.stop_id == identity, Evidence.driver_id == user.driver_id,
        Evidence.captured_at >= route.started_at).order_by(Evidence.captured_at)).all()


@router.get('/evidence/{identity}')
def download(slug: str, identity: UUID, db: DB, user: User = Depends(auth.company_user)):
    row = record(db,Evidence,user,identity)
    stop = record(db,OrderStop,user,row.stop_id)
    order = record(db,Order,user,stop.order_id)
    if user.role == 'SHIPPER' and order.shipper_id != user.shipper_id: raise HTTPException(404,'Evidence not found.')
    if user.role == 'DRIVER' and row.driver_id != user.driver_id: raise HTTPException(404,'Evidence not found.')
    return Response(base64.b64decode(row.content),media_type=row.media_type,
        headers={'Content-Disposition': f'inline; filename="{row.id}.{"png" if row.media_type == "image/png" else "jpg"}"',
            'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


from .schemas import ArrivalCommand


@router.post('/driver/routes/{identity}/stops/{visit_id}/arrive', response_model=RouteView)
def arrive(slug: str, identity: UUID, visit_id: UUID, data: ArrivalCommand, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return execution.arrive_stop(db,user,identity,visit_id,data,idempotency_key)


from .schemas import IssueInput, IssueView

@router.post('/driver/stops/{identity}/issue', response_model=IssueView, status_code=201)
def stop_issue(slug: str, identity: UUID, data: IssueInput, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    from . import issues
    stop = record(db,OrderStop,user,identity)
    return issues.report(db,user,stop.order_id,data.model_copy(update={'stop_id':identity}),idempotency_key)


from app.events.routes import mark_read, page
from .schemas import DriverNotificationView, Version

@router.get('/driver/notifications', response_model=list[DriverNotificationView])
def notifications(slug: str, db: DB, limit: int = Query(100, ge=1, le=200), user: User = Depends(auth.driver)):
    return page(db, user, limit)

@router.post('/driver/notifications/{identity}/read', response_model=DriverNotificationView)
def read_notification(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.driver)):
    return mark_read(db, user, identity, data, idempotency_key, DriverNotificationView)
