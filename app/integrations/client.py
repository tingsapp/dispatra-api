"""Calls to a company's TMS API. Hosts must resolve to public addresses; redirects are never followed.

Errors carry short codes only. Credentials, URLs with query strings and response bodies never reach logs.
"""
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
MAX_BYTES = 5_000_000


class TmsError(Exception):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code, self.status = code, status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


_opener = urllib.request.build_opener(_NoRedirect)


def check_host(url: str):
    """Refuse loopback, private and other non-public destinations (company users enter these URLs)."""
    if os.environ.get('TMS_ALLOW_PRIVATE_HOSTS', '').lower() == 'true': return
    parts = urllib.parse.urlsplit(url)
    try: addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError, TypeError): raise TmsError('HOST_NOT_FOUND') from None
    if not addresses or any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addresses): raise TmsError('HOST_NOT_ALLOWED')


def headers(auth_type: str, credential: str) -> dict[str, str]:
    auth = {'X-API-Key': credential} if auth_type == 'api_key' else {'Authorization': f'Bearer {credential}'}
    return {'Accept': 'application/json', 'User-Agent': 'Dispatra-TMS-Connector/1', **auth}


def url(base: str, path: str, params: dict | None = None) -> str:
    joined = base.rstrip('/') + '/' + path.lstrip('/')
    return joined + ('?' + urllib.parse.urlencode(params) if params else '')


def request(method: str, target: str, auth: dict[str, str], body: dict | None = None):
    """Send one request and return the parsed JSON body (or None when empty)."""
    if urllib.parse.urlsplit(target).scheme != 'https': raise TmsError('HTTPS_REQUIRED')
    check_host(target)
    data = json.dumps(body, separators=(',', ':')).encode() if body is not None else None
    request_ = urllib.request.Request(target, data=data, method=method, headers={**auth, **({'Content-Type': 'application/json'} if data else {})})
    try:
        with _opener.open(request_, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400: raise TmsError('REDIRECT_REFUSED', error.code) from None
        code = {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 429: 'RATE_LIMITED'}.get(error.code, 'SERVER_ERROR' if error.code >= 500 else 'REQUEST_REJECTED')
        raise TmsError(code, error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError): raise TmsError('CONNECTION_FAILED') from None
    if len(raw) > MAX_BYTES: raise TmsError('RESPONSE_TOO_LARGE')
    if not raw.strip(): return None
    try: return json.loads(raw)
    except ValueError: raise TmsError('INVALID_JSON') from None


MAX_PAGES = 50


def fetch_records(base: str, path: str, auth: dict[str, str], params: dict | None = None) -> list[dict]:
    """All records from a list endpoint, following `next` URLs on the same host, `next_cursor` or `next_page`."""
    from .mapping import records
    rows, target, origin = [], url(base, path, params), urllib.parse.urlsplit(base)[:2]
    for _ in range(MAX_PAGES):
        payload = request('GET', target, auth)
        rows += records(payload)
        if not isinstance(payload, dict): return rows
        link = next((payload.get(key) for key in ('next', 'next_url', 'nextUrl') if isinstance(payload.get(key), str) and payload.get(key)), None)
        cursor = next((payload.get(key) for key in ('next_cursor', 'nextCursor') if payload.get(key) not in (None, '')), None)
        page = next((payload.get(key) for key in ('next_page', 'nextPage') if isinstance(payload.get(key), int)), None)
        if link:
            absolute = urllib.parse.urljoin(target, link)
            if urllib.parse.urlsplit(absolute)[:2] != origin: raise TmsError('PAGINATION_HOST')
            target = absolute
        elif cursor is not None: target = url(base, path, {**(params or {}), 'cursor': cursor})
        elif page is not None: target = url(base, path, {**(params or {}), 'page': page})
        else: return rows
    raise TmsError('TOO_MANY_PAGES')
