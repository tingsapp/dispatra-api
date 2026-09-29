import base64
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import execution, dispatch
from .common import record
from .models import Route, Driver, Evidence, OrderStop, Order, DutySession
from .schemas import DutyStart, DutyEnd, DutyView, Telemetry, RouteCommand, StopCommand, EvidenceInput, RouteView, ProofEvidence

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Driver execution'])


from .schemas import DriverProfileView

@router.get('/driver/profile', response_model=DriverProfileView)
def profile(slug: str, db: DB, user: User = Depends(auth.driver)):
    row = record(db,Driver,user,user.driver_id)
    duty = db.scalar(select(DutySession).where(DutySession.organization_id == user.organization_id, DutySession.driver_id == row.id, DutySession.ended_at.is_(None)))
    return {'id': row.id, 'name': row.name, 'email': row.email, 'phone': row.phone,
        'address': row.address, 'service_city': row.service_city, 'vehicle_id': row.vehicle_id,
        'duty': DutyView.model_validate(duty).model_dump(mode='json') if duty else None,
        'location_permission': row.location_permission}


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
        headers={'Content-Disposition': f'attachment; filename="{row.id}.{"png" if row.media_type == "image/png" else "jpg"}"'})


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
