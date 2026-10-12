"""Integration secrets (mailbox app passwords, TMS credentials and webhook secrets) are encrypted at rest with a
server-only key and never returned by the API."""
import os
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

KEY = 'MAILBOX_ENCRYPTION_KEY'


def _cipher():
    key = os.environ.get(KEY, '')
    try: return Fernet(key.encode())
    except ValueError: raise HTTPException(503, 'Credential encryption is not configured on the server.') from None


def seal(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def unseal(token: str, label: str = 'mailbox password') -> str:
    try: return _cipher().decrypt(token.encode()).decode()
    except InvalidToken: raise HTTPException(409, f'Saved {label} cannot be read. Enter it again.') from None
