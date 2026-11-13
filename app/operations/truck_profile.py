"""Shared verified truck facts and coordinates for road providers."""
import math
from fastapi import HTTPException
from pydantic import ValidationError
from .schemas import TruckRoutingProfile

def number(value):
    if isinstance(value, bool): raise ValueError('Invalid routing number')
    result = float(value)
    if not math.isfinite(result) or result < 0: raise ValueError('Invalid routing number')
    return result


def point(address):
    try:
        if isinstance(address['latitude'], bool) or isinstance(address['longitude'], bool): raise ValueError()
        lat, lng = float(address['latitude']), float(address['longitude'])
        if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError()
        return {'latitude': lat, 'longitude': lng}
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, 'Verified stop coordinates are required for truck routing.') from None


def profile(vehicle, *, hazardous=False):
    if hazardous:
        raise HTTPException(409, 'Dangerous goods need dispatcher review for routing.')
    try:
        profile = TruckRoutingProfile.model_validate((vehicle or {}).get('road_profile'))
    except (ValidationError, TypeError):
        raise HTTPException(409, 'Add size and loaded weight to this vehicle’s Vehicle Type.') from None
    if (vehicle or {}).get('payload_kg') is not None and profile.gross_weight_kg < number(vehicle['payload_kg']):
        raise HTTPException(409, 'Loaded gross weight must cover the vehicle payload capacity.')
    return profile
