from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.connectors import verification, transport


def test_account_identity_is_verified_without_mutating_input(monkeypatch):
    credentials = {'api_key': 'private-token'}
    monkeypatch.setattr(transport, 'identity', lambda *args: ('account-1', 'Transport'))
    verified, account, label = verification.verify('truckmate', SimpleNamespace(), credentials)
    assert verified == credentials and verified is not credentials
    assert (account, label) == ('account-1', 'Transport')


def test_rejected_credentials_return_safe_error(monkeypatch):
    def fail(*args): raise transport.Error('UNAUTHORIZED')
    monkeypatch.setattr(transport, 'identity', fail)
    with pytest.raises(HTTPException) as failure:
        verification.verify('truckmate', SimpleNamespace(), {'api_key': 'private-token'})
    assert failure.value.status_code == 400
    assert 'rejected' in failure.value.detail and 'private-token' not in failure.value.detail


def test_cannot_replace_account_on_reconnect(monkeypatch):
    monkeypatch.setattr(transport, 'identity', lambda *args: ('other', 'Other'))
    with pytest.raises(HTTPException) as failure:
        verification.verify('truckmate', SimpleNamespace(), {}, 'original')
    assert failure.value.status_code == 409


def test_client_credentials_exchange_precedes_identity_check(monkeypatch):
    monkeypatch.setattr(transport, 'token', lambda *args: {'access_token': 'verified-token'})
    def identity(provider, profile, credentials):
        assert credentials['access_token'] == 'verified-token'
        return 'account-1', 'Transport'
    monkeypatch.setattr(transport, 'identity', identity)
    assert verification.verify('turvo', SimpleNamespace(), {'client_id':'id','client_secret':'secret'})[1] == 'account-1'
