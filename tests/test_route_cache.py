"""Quota protection: freshness, bounds, safe copies and request coalescing."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.operations.route_cache import RouteCache
from app.operations import openrouteservice as ors


def test_cache_expires_and_does_not_share_mutable_results():
    clock = [0]
    cache = RouteCache(ttl=10, clock=lambda: clock[0])
    calls = []
    def fetch():
        calls.append(1)
        return {'points': [[1, 2]]}
    first = cache.get_or_compute('route', fetch)
    first['points'].clear()
    assert cache.get_or_compute('route', fetch)['points'] == [[1, 2]]
    assert len(calls) == 1
    clock[0] = 11
    cache.get_or_compute('route', fetch)
    assert len(calls) == 2


def test_concurrent_same_route_calls_provider_once():
    cache = RouteCache()
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {'route': 1}
    with ThreadPoolExecutor(4) as pool:
        first = pool.submit(cache.get_or_compute, 'same', fetch)
        assert started.wait(2)
        others = [pool.submit(cache.get_or_compute, 'same', fetch) for _ in range(3)]
        release.set()
        assert first.result() == {'route': 1}
        assert all(f.result() == {'route': 1} for f in others)
    assert len(calls) == 1


def test_failure_is_not_retained_and_lru_is_bounded():
    cache = RouteCache(max_entries=1, max_bytes=100)
    def fail(): raise RuntimeError('temporary')
    with pytest.raises(RuntimeError): cache.get_or_compute('bad', fail)
    assert cache.get_or_compute('bad', lambda: 1) == 1
    cache.get_or_compute('other', lambda: 2)
    assert list(cache.rows) == ['other']
    cache.get_or_compute('large', lambda: 'x' * 200)
    assert 'large' not in cache.rows and cache.bytes <= 100


def test_ors_cache_keys_include_tenant_coordinates_profile_heading_and_key(monkeypatch):
    from app.operations import route_cache
    monkeypatch.setattr(route_cache, 'directions', RouteCache())
    monkeypatch.setenv('ORS_API_KEY', 'test-only')
    calls = []
    def response(payload):
        calls.append(payload)
        return {'features': [{'properties': {'summary': {'duration': 120, 'distance': 1000}},
            'geometry': {'coordinates': [[-123.12, 49.28], [-123.1, 49.27]]}}]}
    monkeypatch.setattr(ors, 'request', response)
    points = [{'latitude': 49.28, 'longitude': -123.12}, {'latitude': 49.27, 'longitude': -123.1}]
    vehicle = {'road_profile': {'length_m': 6, 'width_m': 2, 'height_m': 3, 'gross_weight_kg': 5000}}
    ors.route(points, vehicle, cache_scope='tenant-one')
    ors.route(points, vehicle, cache_scope='tenant-one', geometry=True)
    assert len(calls) == 1
    ors.route(points, vehicle, cache_scope='tenant-two')
    changed = {**vehicle, 'road_profile': {**vehicle['road_profile'], 'height_m': 4}}
    ors.route(points, changed, cache_scope='tenant-one')
    ors.route([{**points[0], 'heading': 90}, points[1]], vehicle, cache_scope='tenant-one')
    ors.route([points[0], {**points[1], 'latitude': 49.26}], vehicle, cache_scope='tenant-one')
    monkeypatch.setenv('ORS_API_KEY', 'replacement-test-key')
    ors.route(points, vehicle, cache_scope='tenant-one')
    assert len(calls) == 6


def test_malformed_provider_result_does_not_poison_cache(monkeypatch):
    from app.operations import route_cache
    from fastapi import HTTPException
    monkeypatch.setattr(route_cache, 'directions', RouteCache())
    calls = []
    monkeypatch.setattr(ors, 'request', lambda payload: calls.append(payload) or {'features': []})
    vehicle = {'road_profile': {'length_m': 6, 'width_m': 2, 'height_m': 3, 'gross_weight_kg': 5000}}
    points = [{'latitude': 49.28, 'longitude': -123.12}, {'latitude': 49.27, 'longitude': -123.1}]
    for _ in range(2):
        with pytest.raises(HTTPException): ors.route(points, vehicle, cache_scope='tenant')
    assert len(calls) == 2


def test_persistent_cache_survives_new_instances_without_extending_expiry(tmp_path):
    from app.operations.route_cache_store import RouteCacheStore
    import hashlib
    clock = [0]
    key = hashlib.sha256(b'restart-sample').hexdigest()
    calls = []
    def fetch():
        calls.append(1)
        return {'points': [[1, 2], [3, 4]]}
    def restarted():
        return RouteCache(ttl=10, clock=lambda: clock[0], store=RouteCacheStore(tmp_path, clock=lambda: clock[0]))
    assert restarted().get_or_compute(key, fetch)['points']
    clock[0] = 8
    cache = restarted()
    cache.get_or_compute(key, fetch)
    assert len(calls) == 1
    clock[0] = 11
    cache.get_or_compute(key, fetch)
    assert len(calls) == 2
    assert tmp_path.stat().st_mode & 0o077 == 0
    assert next(tmp_path.glob('*.json')).stat().st_mode & 0o077 == 0


def test_corrupt_persistent_record_recomputes_and_disk_size_is_bounded(tmp_path):
    from app.operations.route_cache_store import RouteCacheStore
    import hashlib
    store = RouteCacheStore(tmp_path, max_entries=1)
    first = hashlib.sha256(b'first').hexdigest()
    second = hashlib.sha256(b'second').hexdigest()
    store.get_or_compute(first, lambda: {'value': 1}, 60)
    (tmp_path / (first + '.json')).write_text('{broken')
    assert store.get_or_compute(first, lambda: {'value': 2}, 60)[0] == {'value': 2}
    store.get_or_compute(second, lambda: {'value': 3}, 60)
    assert len(list(tmp_path.glob('*.json'))) == 1
    assert (tmp_path / (second + '.json')).exists()
