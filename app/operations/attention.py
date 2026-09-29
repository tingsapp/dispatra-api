"""Derived operational attention; lifecycle and stored history remain unchanged."""
from datetime import datetime

CLOSED = {'COMPLETED', 'INVOICED', 'CANCELLED'}


def flags(order, visits: dict[str, object], observed_at: datetime) -> list[tuple[str, str]]:
    if order.status in CLOSED:
        return []
    result = []
    if order.pricing.get('status') != 'PRICED':
        result.append(('PRICING', order.pricing.get('review_reason') or 'Pricing needs review.'))
    if order.status in {'NEW', 'ASSIGNED'} and order.scheduled_at < observed_at:
        result.append(('LATE_START', 'The scheduled pickup has not started.'))
    for stop in order.facts['stops']:
        if stop['kind'] != 'DROPOFF' or not stop.get('window_end'):
            continue
        visit = visits.get(stop['id'])
        if visit and visit.status == 'COMPLETED':
            continue
        deadline = datetime.fromisoformat(stop['window_end'])
        if deadline < observed_at or visit and visit.planned_at > deadline:
            result.append(('AT_RISK', 'A delivery deadline is missed or the planned arrival is late.'))
            break
    return result
