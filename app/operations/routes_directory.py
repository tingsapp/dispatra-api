from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from app import auth
from app.models import User
from app.routes import DB, OperationKey
from . import directory, archive
from .common import record, settings, operational_shipper
from .models import Catalog, RateCard, Shipper, Driver, Vehicle
from .schemas import (SettingsView, SettingsUpdate, CatalogCreate, CatalogUpdate, CatalogView,
    RateCreate, RateUpdate, RateView, ShipperData, ShipperUpdate, ShipperView, DriverData,
    DriverUpdate, DriverView, VehicleData, VehicleUpdate, VehicleView, Version)

router = APIRouter(prefix='/api/v1/companies/{slug}', tags=['Manual setup'])


def page(db, model, actor, after, limit, include_archived=False):
    query = select(model).where(model.organization_id == actor.organization_id).order_by(model.id).limit(limit)
    if hasattr(model, 'archived_at') and not include_archived:
        query = query.where(model.archived_at.is_(None))
    if model is Shipper: query = query.where(Shipper.warehouse.is_not(None))  # skip legacy account-only records
    if after: query = query.where(model.id > after)
    return db.scalars(query).all()


@router.get('/settings', response_model=SettingsView)
def get_settings(slug: str, db: DB, user: User = Depends(auth.dispatcher)):
    return settings(db,user)[0]


@router.put('/settings', response_model=SettingsView)
def put_settings(slug: str, data: SettingsUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_settings(db,user,data,idempotency_key)


@router.post('/pricing/seed', response_model=dict[str,int])
def seed(slug: str, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    from .seeding import seed_pricing
    from .common import command
    return command(db,user,idempotency_key,'pricing-seed',{},lambda: seed_pricing(db,user))


@router.get('/catalog', response_model=list[CatalogView])
def catalog(slug: str, db: DB, after: UUID | None = None, limit: int = Query(100,ge=1,le=200), user: User = Depends(auth.dispatcher)):
    return page(db,Catalog,user,after,limit)


@router.post('/catalog', response_model=CatalogView, status_code=201)
def create_catalog(slug: str, data: CatalogCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_catalog(db,user,data,idempotency_key)


@router.put('/catalog/{identity}', response_model=CatalogView)
def edit_catalog(slug: str, identity: UUID, data: CatalogUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_catalog(db,user,data,idempotency_key,identity)


@router.delete('/catalog/{identity}', response_model=dict)
def delete_catalog(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.remove_catalog(db,user,identity,data,idempotency_key)


@router.get('/rate-cards', response_model=list[RateView])
def rates(slug: str, db: DB, after: UUID | None = None, limit: int = Query(100,ge=1,le=200), user: User = Depends(auth.dispatcher)):
    return page(db,RateCard,user,after,limit)


@router.post('/rate-cards', response_model=RateView, status_code=201)
def create_rate(slug: str, data: RateCreate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_rate(db,user,data,idempotency_key)


@router.put('/rate-cards/{identity}', response_model=RateView)
def edit_rate(slug: str, identity: UUID, data: RateUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_rate(db,user,data,idempotency_key,identity)


@router.post('/rate-cards/{identity}/archive', response_model=dict[str, str | int])
def archive_rate(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return archive.archive_rate(db, user, identity, data, idempotency_key)


@router.get('/shippers', response_model=list[ShipperView])
def shippers(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=200), include_archived: bool = False, user: User = Depends(auth.dispatcher)):
    return [directory.shipper_view(db,user,row) for row in page(db,Shipper,user,after,limit,include_archived)]


@router.post('/shippers', response_model=ShipperView, status_code=201)
def create_shipper(slug: str, data: ShipperData, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_shipper(db,user,data,idempotency_key)


@router.put('/shippers/{identity}', response_model=ShipperView)
def edit_shipper(slug: str, identity: UUID, data: ShipperUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_shipper(db,user,data,idempotency_key,identity)


@router.post('/shippers/{identity}/archive', response_model=dict[str, str | int])
def archive_shipper(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return archive.archive_shipper(db, user, identity, data, idempotency_key)


@router.get('/vehicles', response_model=list[VehicleView])
def vehicles(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=200), include_archived: bool = False, user: User = Depends(auth.dispatcher)):
    return page(db,Vehicle,user,after,limit,include_archived)


@router.post('/vehicles', response_model=VehicleView, status_code=201)
def create_vehicle(slug: str, data: VehicleData, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_vehicle(db,user,data,idempotency_key)


@router.put('/vehicles/{identity}', response_model=VehicleView)
def edit_vehicle(slug: str, identity: UUID, data: VehicleUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_vehicle(db,user,data,idempotency_key,identity)


@router.post('/vehicles/{identity}/archive', response_model=dict[str, str | int])
def archive_vehicle(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return archive.archive_vehicle(db, user, identity, data, idempotency_key)


@router.get('/drivers', response_model=list[DriverView])
def drivers(slug: str, db: DB, after: UUID | None = None, limit: int = Query(50,ge=1,le=200), include_archived: bool = False, user: User = Depends(auth.dispatcher)):
    return [directory.driver_view(db,user,row) for row in page(db,Driver,user,after,limit,include_archived)]


@router.post('/drivers', response_model=DriverView, status_code=201)
def create_driver(slug: str, data: DriverData, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_driver(db,user,data,idempotency_key)


@router.put('/drivers/{identity}', response_model=DriverView)
def edit_driver(slug: str, identity: UUID, data: DriverUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.save_driver(db,user,data,idempotency_key,identity)


@router.post('/drivers/{identity}/archive', response_model=dict[str, str | int])
def archive_driver(slug: str, identity: UUID, data: Version, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return archive.archive_driver(db, user, identity, data, idempotency_key)


@router.get('/shippers/{identity}', response_model=ShipperView)
def shipper(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    return directory.shipper_view(db,user,operational_shipper(db,user,identity))


@router.get('/drivers/{identity}', response_model=DriverView)
def driver(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    return directory.driver_view(db,user,record(db,Driver,user,identity))


@router.get('/vehicles/{identity}', response_model=VehicleView)
def vehicle(slug: str, identity: UUID, db: DB, user: User = Depends(auth.dispatcher)):
    return record(db,Vehicle,user,identity)


@router.post('/drivers/{identity}/reset-password', response_model=dict)
def reset_driver(slug: str, identity: UUID, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.dispatcher)):
    return directory.reset_driver_password(db,user,identity,idempotency_key)


@router.get('/shipper/profile', response_model=ShipperView)
def own_shipper(slug: str, db: DB, user: User = Depends(auth.customer)):
    return directory.shipper_view(db,user,operational_shipper(db,user,user.shipper_id))


from .schemas import ShipperProfileUpdate


@router.patch('/shipper/profile', response_model=ShipperView)
def own_shipper_update(slug: str, data: ShipperProfileUpdate, db: DB, idempotency_key: OperationKey, user: User = Depends(auth.customer)):
    return directory.update_shipper_profile(db,user,data,idempotency_key)
