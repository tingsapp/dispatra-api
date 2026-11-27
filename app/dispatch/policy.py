"""One candidate order shared by proposals, AI ranking and AUTO assignment: morning home zone, then route consolidation."""

def zone_tier(candidate):
    """Morning pass: vehicles parked outside the Order's zone come after every feasible home-zone vehicle, however far."""
    return 1 if candidate['metrics'].get('home_zone_match') is False else 0


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


def tier(candidate, candidates):
    """(zone tier, consolidation priority), consolidation compared only within the same zone tier."""
    zone = zone_tier(candidate)
    return zone, priority(candidate, [c for c in candidates if zone_tier(c) == zone])


def ordered(candidates):
    return sorted(candidates, key=lambda c: (*tier(c, candidates), c['score'], c['driver_number']))


def prefer_proposal(candidates, driver_id):
    """An estimated fleet proposal cannot displace a better road-verified tier."""
    if not candidates or not driver_id:
        return candidates
    best = min(tier(c, candidates) for c in candidates)
    threshold = min(c['score'] for c in candidates if tier(c, candidates) == best) + 5
    return sorted(candidates, key=lambda c: not (c['driver_id'] == driver_id
                  and tier(c, candidates) == best and c['score'] <= threshold))
