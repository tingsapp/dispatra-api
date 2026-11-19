"""The Monitor's reporting day is the company's local scheduled pickup day."""
from datetime import datetime, time, timedelta


def day_bounds(observed, zone):
    day = observed.astimezone(zone).date()
    return (datetime.combine(day, time.min, zone),
            datetime.combine(day + timedelta(days=1), time.min, zone))


def due_pickup(order, visits, observed):
    """Scheduled readiness is due work, not evidence of a missed arrival deadline."""
    if order.status not in ('NEW', 'ASSIGNED') or order.scheduled_at > observed:
        return None
    pickups = [stop for stop in order.facts['stops'] if stop['kind'] == 'PICKUP']
    pending = [stop for stop in pickups if not ((visit := visits.get(str(stop['id']))) and
        (visit.arrived_at is not None or visit.status == 'COMPLETED'))]
    if not pending:
        return None
    return {'id': None, 'order_id': str(order.id), 'kind': 'DUE',
        'description': 'Scheduled pickup time reached; pickup is still pending.', 'at': order.scheduled_at}


def alert_time(order, flag, observed, visits=None):
    """Use the missed deadline for derived alerts instead of the order creation time."""
    kind = {'LATE_START': 'PICKUP', 'AT_RISK': 'DROPOFF'}.get(flag)
    visits = visits or {}
    def pending(stop):
        visit = visits.get(str(stop['id']))
        return not visit or (visit.status != 'COMPLETED' and (kind != 'PICKUP' or visit.arrived_at is None))
    deadlines = [datetime.fromisoformat(stop['window_end']) for stop in order.facts['stops']
        if stop['kind'] == kind and stop.get('window_end') and pending(stop)]
    passed = [at for at in deadlines if at <= observed]
    return min(passed) if passed else getattr(order, 'updated_at', order.created_at)
