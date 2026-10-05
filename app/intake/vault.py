"""Mailbox app passwords are encrypted at rest with a server-only key and never returned by the API."""
import os
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

KEY = 'MAILBOX_ENCRYPTION_KEY'


def _cipher():
    key = os.environ.get(KEY, '')
    try: return Fernet(key.encode())
    except ValueError: raise HTTPException(503, 'Mailbox encryption is not configured on the server.') from None


def seal(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def unseal(token: str) -> str:
    try: return _cipher().decrypt(token.encode()).decode()
    except InvalidToken: raise HTTPException(409, 'Saved mailbox password cannot be read. Enter it again.') from None
