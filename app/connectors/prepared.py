"""Documented connection defaults, not certified import mappings or live credentials.

Rose Rocket: https://roserocket.readme.io/docs/rose-rocket-api-oauth-20-authentication-guide
CartonCloud: https://api-docs.cartoncloud.com/latest/guides/authentication
Account scope: https://api-docs.cartoncloud.com/latest/guides/account-info

ISV connections use authorization code flow for both vendors. CartonCloud must supply
the registered ISV authorization URL. No endpoint, data map or status write is guessed.
"""
from copy import deepcopy

PREPARED = {
    'rose-rocket': {
        'base_url': 'https://network.roserocket.com',
        'authorize_url': 'https://a.roserocket.com/authorize',
        'token_url': 'https://a.roserocket.com/oauth/token',
        'client_id_env': 'ROSE_ROCKET_CLIENT_ID',
        'client_secret_env': 'ROSE_ROCKET_CLIENT_SECRET',
        'scopes': 'offline_access email profile',
        'audience': 'https://roserocket.com',
        'token_format': 'form', 'token_auth': 'body',
        'identity': {'path': '/api/v1/me'},
    },
    'cartoncloud': {
        'base_url': 'https://api.cartoncloud.com',
        'token_url': 'https://api.cartoncloud.com/uaa/oauth/token',
        'client_id_env': 'CARTONCLOUD_CLIENT_ID',
        'client_secret_env': 'CARTONCLOUD_CLIENT_SECRET',
        'token_format': 'form', 'token_auth': 'basic',
        'headers': {'Accept-Version': '8'},
        'identity': {'path': '/users/me'},
        'identity_scope': 'cartoncloud_tenant',
        'account_id_path': '', 'account_label_path': '',
    },
}

def prepare(provider: str, supplied: dict) -> dict:
    """Apply operator overrides to known defaults; Profile still requires reviewed maps."""
    result = {**deepcopy(PREPARED.get(provider, {})), **supplied}
    result['headers'] = {**PREPARED.get(provider, {}).get('headers', {}), **supplied.get('headers', {})}
    return result
