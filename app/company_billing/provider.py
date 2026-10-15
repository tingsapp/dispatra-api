"""Bounded Stripe API transport. Never return provider payloads or secrets in errors."""
import os
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException


def origin():
    value = os.environ.get('BILLING_WEB_ORIGIN', '').rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'})) or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise HTTPException(503, 'Payment setup is unavailable. Contact Dispatra support.')
    return value


def available():
    try:
        origin()
        return bool(os.environ.get('STRIPE_SECRET_KEY', '').startswith('sk_test_') and os.environ.get('STRIPE_WEBHOOK_SECRET'))
    except HTTPException:
        return False


def call(method, path, data=None, key=None, *, missing_ok=False):
    secret = os.environ.get('STRIPE_SECRET_KEY')
    if not secret or not secret.startswith('sk_test_'): raise HTTPException(503, 'Payment setup is unavailable. Contact Dispatra support.')
    headers = {'Authorization': 'Bearer ' + secret, 'Stripe-Version': '2025-06-30.basil'}
    if key: headers['Idempotency-Key'] = key
    try:
        response = httpx.request(method, 'https://api.stripe.com/v1/' + path, data=data, headers=headers, timeout=15, follow_redirects=False)
        if missing_ok and method == 'GET' and response.status_code == 404: return None
        if response.status_code >= 400: raise ValueError('Provider rejected request')
        payload = response.json()
        if not isinstance(payload, dict): raise ValueError('Invalid provider response')
        return payload
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Unable to reach payment setup. Please try again.') from None


def checkout_url(value):
    parsed = urlsplit(value or '')
    if parsed.scheme != 'https' or parsed.hostname != 'checkout.stripe.com' or parsed.username or parsed.port not in (None, 443):
        raise HTTPException(503, 'Unable to open payment setup. Please try again.')
    return value
