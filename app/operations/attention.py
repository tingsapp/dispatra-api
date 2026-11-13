"""Derived operational attention; lifecycle and stored history remain unchanged."""
from datetime import datetime

CLOSED = {'COMPLETED', 'CANCELLED'}


def flags(order, visits: dict[str, object], observed_at: datetime) -> list[tuple[str, str]]:
    if order.status in CLOSED:
        return []
    result = []
    if order.pricing.get('status') != 'PRICED':
        result.append(('PRICING', order.pricing.get('review_reason') or 'Pricing needs review.'))
    if order.status in {'NEW', 'ASSIGNED'}:
        for stop in order.facts['stops']:
            visit = visits.get(stop['id'])
            if (stop['kind'] == 'PICKUP' and stop.get('window_end')
                and datetime.fromisoformat(stop['window_end']) < observed_at
                and not (visit and (visit.arrived_at is not None or visit.status == 'COMPLETED'))):
                result.append(('LATE_START', 'Pickup deadline passed; no arrival.'))
                break
    for stop in order.facts['stops']:
        if stop['kind'] != 'DROPOFF' or not stop.get('window_end'):
            continue
        visit = visits.get(stop['id'])
        if visit and visit.status == 'COMPLETED':
            continue
        deadline = datetime.fromisoformat(stop['window_end'])
        if deadline < observed_at or visit and visit.planned_at > deadline:
            result.append(('AT_RISK', 'Delivery deadline missed or arrival late.'))
            break
    return result
