"""One route-consolidation priority shared by proposals, AI ranking and AUTO assignment."""

def priority(candidate, candidates):
    """Prefer a feasible existing-route insertion when it uses no more driving than a new route.

    Capacity, timing, duty, custody and road restrictions are checked by the planner
    before candidates reach this policy. Workload only breaks ties within a tier.
    """
    metrics = candidate['metrics']
    if not metrics.get('route_orders') or metrics.get('awaiting_duty'):
        return 1
    standalone = [c['metrics']['extra_minutes'] for c in candidates
                  if not c['metrics'].get('route_orders') and not c['metrics'].get('awaiting_duty')]
    return 0 if not standalone or metrics['extra_minutes'] <= min(standalone) else 1


def ordered(candidates):
    return sorted(candidates, key=lambda c: (priority(c, candidates), c['score'], c['driver_number']))


def prefer_proposal(candidates, driver_id):
    """An estimated fleet proposal cannot displace a better road-verified priority tier."""
    if not candidates or not driver_id:
        return candidates
    tier = min(priority(c, candidates) for c in candidates)
    threshold = min(c['score'] for c in candidates if priority(c, candidates) == tier) + 5
    return sorted(candidates, key=lambda c: not (c['driver_id'] == driver_id
                  and priority(c, candidates) == tier and c['score'] <= threshold))
