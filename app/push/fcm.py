"""Firebase Cloud Messaging HTTP v1 client using a Google service account (no SDK dependency).

`FIREBASE_SERVICE_ACCOUNT_FILE` points to the service-account JSON kept only on the server. The OAuth access
token comes from a signed RS256 JWT and is cached until shortly before it expires. Results:
`SENT`, `UNREGISTERED` (drop the token), `REJECTED` (do not retry) or `RETRY`.
"""
import base64
import json
import os
import time
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
TIMEOUT = 15


def _b64(raw: bytes) -> str: return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class Credentials:
    def __init__(self, info: dict):
        missing = [key for key in ('project_id', 'client_email', 'private_key') if not info.get(key)]
        if missing: raise ValueError('Firebase service account is missing ' + ', '.join(missing) + '.')
        self.project_id, self.email = info['project_id'], info['client_email']
        self.token_uri = info.get('token_uri') or 'https://oauth2.googleapis.com/token'
        self._key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
        self._token, self._expires = None, 0.0

    @classmethod
    def from_env(cls):
        path = os.environ.get('FIREBASE_SERVICE_ACCOUNT_FILE', '').strip()
        if not path: return None
        with open(path, encoding='utf-8') as handle: return cls(json.load(handle))

    def assertion(self, issued: int) -> str:
        header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
        claims = _b64(json.dumps({'iss': self.email, 'scope': SCOPE, 'aud': self.token_uri, 'iat': issued, 'exp': issued + 3600}).encode())
        signature = self._key.sign(f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f'{header}.{claims}.{_b64(signature)}'

    def access_token(self, client: httpx.Client) -> str:
        if self._token and time.time() < self._expires - 60: return self._token
        issued = int(time.time())
        response = client.post(self.token_uri, data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': self.assertion(issued)}, timeout=TIMEOUT)
        response.raise_for_status()
        body = response.json()
        self._token, self._expires = body['access_token'], issued + int(body.get('expires_in', 3600))
        return self._token


def _error_code(response) -> str:
    try: details = response.json().get('error', {}).get('details', [])
    except ValueError: return ''
    return next((item.get('errorCode', '') for item in details if item.get('errorCode')), '')


def send(credentials: Credentials, client: httpx.Client, token: str, title: str, body: str, data: dict) -> str:
    message = {'message': {'token': token, 'notification': {'title': title, 'body': body}, 'data': data,
        'android': {'priority': 'HIGH'}, 'apns': {'headers': {'apns-priority': '10'}}}}
    try:
        response = client.post(f'https://fcm.googleapis.com/v1/projects/{credentials.project_id}/messages:send',
            headers={'Authorization': f'Bearer {credentials.access_token(client)}'}, json=message, timeout=TIMEOUT)
    except httpx.HTTPError:
        return 'RETRY'
    if response.status_code == 200: return 'SENT'
    code = _error_code(response)
    if response.status_code == 404 or code == 'UNREGISTERED': return 'UNREGISTERED'
    if response.status_code == 429 or response.status_code >= 500 or code in {'UNAVAILABLE', 'INTERNAL', 'QUOTA_EXCEEDED'}: return 'RETRY'
    if response.status_code in {401, 403}: return 'RETRY'
    return 'REJECTED'
