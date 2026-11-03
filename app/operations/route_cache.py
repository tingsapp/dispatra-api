"""Bounded, tenant-keyed memory cache backed by private persistent storage for static development road results.

Concurrent identical requests share one provider call. Failures are shared only
with current waiters, never retained for later retries. No secrets are stored.
"""
import copy
import json
import threading
import time
import os
from pathlib import Path
from collections import OrderedDict


class RouteCache:
    def __init__(self, *, ttl=3600, max_entries=256, max_bytes=16_000_000, clock=time.monotonic, store=None):
        self.store = store
        self.ttl, self.max_entries, self.max_bytes, self.clock = ttl, max_entries, max_bytes, clock
        self.rows, self.flights = OrderedDict(), {}
        self.bytes = 0
        self.lock = threading.RLock()

    def get_or_compute(self, key, compute):
        with self.lock:
            row = self.rows.get(key)
            if row is not None:
                expires, size, value = row
                if self.clock() < expires:
                    self.rows.move_to_end(key)
                    return copy.deepcopy(value)
                self.bytes -= size
                del self.rows[key]
            flight = self.flights.get(key)
            owner = flight is None
            if owner:
                flight = {'ready': threading.Event()}
                self.flights[key] = flight
        if not owner:
            flight['ready'].wait()
            if 'error' in flight: raise flight['error']
            return copy.deepcopy(flight['value'])
        try:
            if self.store is None:
                value, lifetime = compute(), self.ttl
            else:
                value, lifetime = self.store.get_or_compute(key, compute, self.ttl)
            saved = copy.deepcopy(value)
            size = len(json.dumps(saved, separators=(',', ':')).encode())
            with self.lock:
                flight['value'] = saved
                if size <= self.max_bytes:
                    self.rows[key] = (self.clock() + lifetime, size, saved)
                    self.bytes += size
                    while len(self.rows) > self.max_entries or self.bytes > self.max_bytes:
                        _, (_, removed, _) = self.rows.popitem(last=False)
                        self.bytes -= removed
            return value
        except BaseException as error:
            with self.lock: flight['error'] = error
            raise
        finally:
            with self.lock:
                self.flights.pop(key, None)
                flight['ready'].set()


# Static ORS road responses only: never live traffic, decisions, prices or authentication.
from .route_cache_store import RouteCacheStore

_default_directory = Path(__file__).resolve().parents[2] / '.local' / 'route-cache'
directions = RouteCache(store=RouteCacheStore(os.environ.get('ORS_ROUTE_CACHE_DIR', str(_default_directory))))
