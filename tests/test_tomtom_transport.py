"""Provider errors must be actionable without exposing provider response bodies."""
import io
import urllib.error
import pytest
from fastapi import HTTPException
from app.operations import tomtom


def test_exhausted_credits_are_reported_without_provider_secrets(monkeypatch):
    monkeypatch.setenv('TOMTOM_API_KEY', 'private-test-key')
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://provider.invalid/?key=private-test-key', 403, 'Forbidden', {},
            io.BytesIO(b'{"detailedError":{"code":"InsufficientFunds","message":"private-test-key"}}'))
    monkeypatch.setattr(tomtom.urllib.request, 'urlopen', fail)
    with pytest.raises(HTTPException) as caught:
        tomtom.request('/routing/matrix/2', {}, {})
    assert caught.value.status_code == 503
    assert 'credits are exhausted' in caught.value.detail
    assert 'private-test-key' not in caught.value.detail


def test_other_provider_errors_remain_sanitized(monkeypatch):
    monkeypatch.setenv('TOMTOM_API_KEY', 'private-test-key')
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://provider.invalid', 403, 'Forbidden', {}, io.BytesIO(b'private-test-key'))
    monkeypatch.setattr(tomtom.urllib.request, 'urlopen', fail)
    with pytest.raises(HTTPException) as caught:
        tomtom.request('/routing/matrix/2', {}, {})
    assert caught.value.status_code == 503
    assert caught.value.detail == 'TomTom routing is unavailable. Try again.'
