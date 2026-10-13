"""Facts and qualified calculations. Never infer waiting cause, arrival, or road ETA from proximity."""
import math
from datetime import datetime, timedelta


def instant(value):
    if not value: return None
    try: result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (ValueError,TypeError): return None
    return result if result.tzinfo is not None else None


def fact(label, value, source, at=None):
    return dict(label=label,value=str(value),source=source,at=at.isoformat() if at else None)


def location_usable(location, duty, current):
    # Native sends during active tracking; reject stale, future, inaccurate or closed-duty samples.
    if not location or not duty or duty.ended_at is not None: return False
    accuracy = location.data.get('accuracy_m')
    if isinstance(accuracy,bool): return False
    try: accuracy = float(accuracy)
    except (ValueError,TypeError): return False
    return (math.isfinite(accuracy) and 0 < accuracy <= 75
        and duty.started_at <= location.captured_at <= current
        and current-location.captured_at <= timedelta(minutes=2))


def separation(location, address):
    lat,lng=address.get('latitude'),address.get('longitude')
    if lat is None or lng is None: return None
    a,b=math.radians(float(location.data['latitude'])),math.radians(float(lat))
    dlat,dlng=b-a,math.radians(float(lng)-float(location.data['longitude']))
    angle=2*math.asin(min(1,math.sqrt(math.sin(dlat/2)**2+math.cos(a)*math.cos(b)*math.sin(dlng/2)**2)))
    return round(6_371_000*angle)


def deadline(order, stop):
    end = instant(stop.data.get('window_end'))
    if end: return end
    first = next((s for s in order.facts['stops'] if s['kind']=='PICKUP'),None)
    return order.scheduled_at if first and str(stop.id)==first['id'] else None
