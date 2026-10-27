"""Resolve the fleet vehicle's own company Vehicle Type for every truck-routing consumer."""
from sqlalchemy import select
from fastapi import HTTPException
from .models import Catalog


def vehicle_data(db, organization_id, vehicle):
    kind = db.scalar(select(Catalog).where(Catalog.organization_id == organization_id,
        Catalog.id == vehicle.type_id, Catalog.kind == 'VEHICLE_TYPE'))
    if kind is None: raise HTTPException(409, 'The vehicle needs a valid Vehicle Type for truck routing.')
    result = dict(vehicle.data)
    # A type profile (including an explicit clear) supersedes old per-vehicle measurements.
    # Legacy vehicle facts remain readable until that type is configured; no guessed backfill.
    if 'road_profile' in kind.data: result['road_profile'] = kind.data['road_profile']
    return result
