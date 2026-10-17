"""Bounded fleet planning. Tentative allocations never mutate Orders or bypass assignment checks."""
from collections import defaultdict
from .candidates import evaluate, blocker
from .geography import delivery_zone
from app.operations.common import settings


def plan(db, actor, orders):
    _, config = settings(db, actor)
    allocations, result = defaultdict(list), []
    # Scarce/special requirements first, then geography and booked time, with all eligible drivers compared.
    pending = []
    for order in orders:
        if blocker(order): continue
        candidates, excluded = evaluate(db, actor, order, road=False, include_expected=True)
        pending.append((len(candidates), order, candidates, excluded))
    pending.sort(key=lambda row:(row[0], min((s.get('window_end') or '9999' for s in row[1].facts['stops'] if s['kind']=='DROPOFF'),default='9999'),
        delivery_zone(row[1],config) or '',row[1].scheduled_at,str(row[1].id)))
    for _, order, _, _ in pending:
        candidates, excluded = evaluate(db, actor, order, allocations, road=False, include_expected=True)
        if candidates: allocations[candidates[0]['driver_id']].append(order)
        result.append((order,candidates,excluded))
    return result
