"""Verify account access before committing a credential connection; never expose vendor errors."""
from fastapi import HTTPException
from . import transport
from .catalog import BY_ID


def verify(provider, profile, credentials, expected_account=None):
    try:
        verified = dict(credentials)
        if BY_ID[provider].auth == 'client_credentials':
            verified['access_token'] = transport.token(profile, {'grant_type': 'client_credentials'}, credentials)['access_token']
        account, label = transport.identity(provider, profile, verified)
        if expected_account and account != expected_account:
            raise HTTPException(409, 'These credentials belong to a different account. Disconnect before changing accounts.')
        return verified, account, label
    except transport.Error as failure:
        message = {
            'UNAUTHORIZED': 'The credentials were rejected. Check your token and try again.',
            'FORBIDDEN': 'The token does not have the required read permissions.',
            'ACCOUNT_SELECTION_REQUIRED': 'The provider account must be selected before connecting.',
        }.get(failure.code, 'Connection test failed. Check the API address, credentials and account access, then try again.')
        raise HTTPException(400, message) from None
