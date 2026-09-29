"""Google road metrics requested only for explicit pricing and planning commands.

No external requests in demo mode. Google keys are server-only environment secrets.
"""
import json
import os
import urllib.request
import urllib.error
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from fastapi import HTTPException


def provider():
    return os.environ.get('ROUTING_PROVIDER','disabled')


def waypoint(address):
    if address.get('latitude') is None or address.get('longitude') is None:
        raise HTTPException(409,'Verified stop coordinates are required for road routing.')
    return {'location':{'latLng':{'latitude':address['latitude'],'longitude':address['longitude']}}}


def google(db, actor, method, payload, mask):
    key = os.environ.get('GOOGLE_ROUTES_API_KEY')
    if provider() != 'google' or not key: raise HTTPException(409,'Road routing is not configured. Dispatcher pricing review is required.')
    request = urllib.request.Request('https://routes.googleapis.com/' + method,data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','X-Goog-Api-Key':key,'X-Goog-FieldMask':mask},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=15) as response:
            result=json.loads(response.read(2_000_000))
    except (OSError,ValueError):
        raise HTTPException(503,'Road routing provider is unavailable. Retry or request dispatcher review.') from None
    return result


def matrix(db, actor, stops):
    if provider() == 'demo': return None
    if len(stops) > 25: raise HTTPException(409,'Road matrix is limited to 25 stops per manual Route; split the Route.')
    points=[{'waypoint':waypoint(s['address'])} for s in stops]
    result=google(db,actor,'distanceMatrix/v2:computeRouteMatrix',
        {'origins':points,'destinations':points,'travelMode':'DRIVE','routingPreference':'TRAFFIC_UNAWARE'},
        'originIndex,destinationIndex,status,condition,duration')
    table={}
    if not isinstance(result,list): raise HTTPException(503,'Road routing returned an invalid matrix.')
    for cell in result:
        if cell.get('status',{}).get('code',0) != 0 or cell.get('condition') != 'ROUTE_EXISTS': continue
        try:
            origin,destination=stops[cell.get('originIndex',0)]['id'],stops[cell.get('destinationIndex',0)]['id']
            seconds=float(cell['duration'].removesuffix('s'))
            if seconds < 0: raise ValueError()
            table[(origin,destination)]=seconds/60
        except (KeyError,IndexError,ValueError,TypeError): raise HTTPException(503,'Road routing returned incomplete travel times.') from None
    return table


def order_route(db, actor, booking, mask):
    """One Routes call over the Order's own stops in precedence order (pickups before the deliveries they supply)."""
    pending=list(booking.stops);ordered=[];visited=set()
    while pending:
        ready=next((s for s in pending if all(i.pickup_id in visited for i in booking.items if i.delivery_id==s.id)),None)
        if ready is None: raise HTTPException(422,'Order stop precedence is invalid.')
        ordered.append(ready);visited.add(ready.id);pending.remove(ready)
    if len(ordered)>27: raise HTTPException(409,'Standalone road pricing supports at most 27 stops; review the booking.')
    return google(db,actor,'directions/v2:computeRoutes',
        {'origin':waypoint(ordered[0].address.model_dump()),'destination':waypoint(ordered[-1].address.model_dump()),
         'intermediates':[waypoint(s.address.model_dump()) for s in ordered[1:-1]],'travelMode':'DRIVE','routingPreference':'TRAFFIC_UNAWARE'},
        mask)


def decode_polyline(encoded):
    """Google encoded polyline (precision 5) to [lat, lng] pairs."""
    points=[];index=lat=lng=0
    while index<len(encoded):
        for axis in (0,1):
            shift=result=0
            while True:
                byte=ord(encoded[index])-63;index+=1
                result|=(byte&0x1f)<<shift;shift+=5
                if byte<0x20: break
            delta=~(result>>1) if result&1 else result>>1
            if axis==0: lat+=delta
            else: lng+=delta
        points.append([lat/1e5,lng/1e5])
    return points


def order_road_path(db, actor, booking):
    """Road geometry for drawing the Order on the map; never used for pricing."""
    if provider() != 'google': raise HTTPException(409,'Road routing is not configured.')
    result=order_route(db,actor,booking,'routes.polyline.encodedPolyline')
    try: return decode_polyline(result['routes'][0]['polyline']['encodedPolyline'])
    except (KeyError,IndexError,TypeError,ValueError): raise HTTPException(409,'No road route was found for this Order.') from None


def order_travel(db, actor, booking):
    """Standalone road distance (km) and driving minutes for the Order's own stops, in precedence order; one Routes call."""
    if provider() != 'google': raise HTTPException(409,'Verified standalone Order distance is required for pricing review.')
    result=order_route(db,actor,booking,'routes.distanceMeters,routes.duration')
    try:
        route=result['routes'][0]
        meters=Decimal(route['distanceMeters']); seconds=Decimal(route['duration'].removesuffix('s'))
    except (KeyError,IndexError,ValueError,TypeError,AttributeError,ArithmeticError): raise HTTPException(409,'No road route was found for pricing.') from None
    if meters<0 or seconds<0: raise HTTPException(503,'Road routing returned an invalid distance.')
    return (meters/1000).quantize(Decimal('0.01'), ROUND_HALF_UP), max(1, int((seconds/60).to_integral_value(rounding=ROUND_CEILING)))
