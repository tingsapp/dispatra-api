"""Small Stripe REST boundary. Card numbers and CVC never pass through Dispatra."""
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from fastapi import HTTPException

API_VERSION = '2025-02-24.acacia'


def configured():
    return bool(re.fullmatch(r'(?:sk|rk)_(?:test|live)_[A-Za-z0-9]+', os.environ.get('STRIPE_SECRET_KEY', '')))


def livemode():
    return '_live_' in os.environ.get('STRIPE_SECRET_KEY', '')


def request(method, path, *, account=None, data=None, key=None):
    if not configured(): raise HTTPException(409, 'Credit card setup is not configured for this company.')
    headers = {'Authorization': 'Bearer ' + os.environ['STRIPE_SECRET_KEY'], 'Stripe-Version': API_VERSION}
    if account: headers['Stripe-Account'] = account
    if key: headers['Idempotency-Key'] = key
    encoded = urlencode(data or {}).encode()
    url = 'https://api.stripe.com/v1/' + path
    if method == 'GET' and encoded: url += '?' + encoded.decode()
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        with urlopen(Request(url, data=encoded if method == 'POST' else None, headers=headers, method=method), timeout=20) as response:
            value = json.loads(response.read(2_000_000))
        if not isinstance(value, dict): raise ValueError()
        return value
    except HTTPError as error:
        if error.code == 409: raise HTTPException(409, 'Card setup is already being processed. Please retry shortly.') from None
        raise HTTPException(503, 'Stripe could not complete this request. Please try again or contact your dispatch company.') from None
    except (OSError, ValueError):
        raise HTTPException(503, 'Stripe is unavailable. Please try again.') from None
