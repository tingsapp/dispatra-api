"""Read-only Samsara account verification. Token is sent only to the fixed official host."""
from fastapi import HTTPException
from app.integrations import client

REVISION = 'samsara-account-v1'


def identity(credentials):
    if set(credentials) != {'api_key'}:
        raise HTTPException(422, 'Enter your Samsara API token.')
    token = credentials['api_key']
    if not token or len(token) > 2048 or token != token.strip() or any(c.isspace() for c in token):
        raise HTTPException(422, 'Enter a valid Samsara API token without spaces.')
    try:
        result = client.request('GET', 'https://api.samsara.com/me', client.headers('bearer', token))
    except client.TmsError as failure:
        message = {
            'UNAUTHORIZED': 'Samsara rejected this token. Check that it is valid and has not been revoked.',
            'FORBIDDEN': 'Enable Read Org Information under Setup & Administration for this Samsara token.',
            'RATE_LIMITED': 'Samsara is limiting requests. Wait a moment and test again.',
        }.get(failure.code, 'Could not verify the Samsara account. Please try again.')
        raise HTTPException(400, message) from None
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not data['id'].strip() or len(data['id']) > 200:
        raise HTTPException(400, 'Samsara returned an unexpected account response. The connection was not saved.')
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise HTTPException(400, 'Samsara returned an unexpected account response. The connection was not saved.')
    return data['id'], name
