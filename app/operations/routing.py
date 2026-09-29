"""Deterministic planning with precedence, physical capacity and time windows.

Travel uses a supplied server-side matrix when configured. The built-in demo
estimator is explicitly identified; it never supplies authoritative quote distance.
"""
import math
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import permutations
from fastapi import HTTPException


def travel_minutes(a, b):
    if any(p.get('latitude') is None or p.get('longitude') is None for p in (a,b)):
        raise HTTPException(409, 'Every stop needs verified coordinates for route planning.')
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b['longitude'] - a['longitude'])
    h = math.sin(delta_lat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(delta_lon/2)**2
    km = 6371 * 2 * math.asin(min(1, math.sqrt(h)))
    return km / 35 * 60


def fits_item(item, vehicle):
    dims = [Decimal(str(item[k])) for k in ['length_cm','width_cm','height_cm']]
    bay = [Decimal(str(vehicle[k])) for k in ['length_cm','width_cm','height_cm']]
    return any(all(a <= b for a,b in zip(rotation,bay)) for rotation in permutations(dims))


def optimize(stops, items, vehicle, start, end, max_work_minutes, travel_table=None):
    """Bounded search finds the shortest estimated feasible sequence among explored paths."""
    stop_map = {s['id']: s for s in stops}
    if vehicle.get('maximum_stops') and len(stops) > vehicle['maximum_stops']:
        raise HTTPException(409, 'Route exceeds vehicle Maximum stops.')
    if any(not fits_item(item, vehicle) for item in items): raise HTTPException(409, 'A package does not fit the vehicle dimensions.')
    for stop in stops:
        travel_minutes(stop['address'], stop['address'])
    capacities = (Decimal(str(vehicle['payload_kg'])), Decimal(str(vehicle['volume_m3'])), Decimal(vehicle.get('pallet_capacity', 0)))
    pickup_items = {s['id']: [i for i in items if i['pickup_id'] == s['id']] for s in stops}
    delivery_items = {s['id']: [i for i in items if i['delivery_id'] == s['id']] for s in stops}
    deadlines = [start + timedelta(minutes=max_work_minutes)]
    if end: deadlines.append(end)
    deadline = min(deadlines)
    best = None
    best_score = float('inf')
    explored = 0
    budget = 15000

    def visit(path, remaining, clock, load, score, schedule):
        nonlocal best, best_score, explored
        explored += 1
        if explored > budget or score >= best_score: return
        if not remaining:
            best, best_score = list(schedule), score
            return
        candidates = []
        for identity in remaining:
            stop = stop_map[identity]
            if any(i['pickup_id'] not in path for i in delivery_items[identity]): continue
            duration = (travel_table.get((path[-1],identity)) if travel_table is not None else travel_minutes(stop_map[path[-1]]['address'], stop['address'])) if path else 0
            if duration is None: continue
            arrival = clock + timedelta(minutes=duration)
            if stop.get('window_start'): arrival = max(arrival, datetime.fromisoformat(stop['window_start']))
            if stop.get('window_end') and arrival > datetime.fromisoformat(stop['window_end']): continue
            finish = arrival + timedelta(minutes=stop.get('service_minutes', 0))
            if finish > deadline: continue
            next_load = list(load)
            for sign, cargo in [(1,pickup_items[identity]),(-1,delivery_items[identity])]:
                for item in cargo:
                    q = Decimal(item['quantity'])
                    next_load[0] += sign * q * Decimal(str(item['weight_kg']))
                    next_load[1] += sign * q * Decimal(str(item['length_cm'])) * Decimal(str(item['width_cm'])) * Decimal(str(item['height_cm'])) / 1000000
                    next_load[2] += sign * q * Decimal(item.get('pallets', 0))
            if any(value < 0 or value > capacity for value,capacity in zip(next_load,capacities)): continue
            candidates.append((duration, identity, arrival, finish, next_load))
        for duration, identity, arrival, finish, next_load in sorted(candidates, key=lambda value: (value[0],value[1])):
            visit(path + [identity], remaining - {identity}, finish, next_load, score + duration,
                schedule + [{'stop_id': identity, 'planned_at': arrival.isoformat(), 'load_kg': str(next_load[0]), 'volume_m3': str(next_load[1]), 'pallets': str(next_load[2])}])
    visit([], set(stop_map), start, [Decimal(0)]*3, 0, [])
    if best is None: raise HTTPException(409, 'No feasible Route found within the planning limit; review capacity, windows and shift.')
    return {'stops': best, 'travel_minutes': round(best_score,2), 'travel_basis': 'GOOGLE_ROADS_NO_TRAFFIC' if travel_table is not None else 'DEMO_GEODESIC_35_KPH',
        'is_road_eta': travel_table is not None, 'search_complete': explored <= budget, 'explored': min(explored,budget), 'planned_at': start.isoformat()}
