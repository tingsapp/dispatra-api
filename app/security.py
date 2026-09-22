import hashlib
import secrets
from datetime import timedelta
from fastapi import HTTPException
from pwdlib import PasswordHash
from sqlalchemy import text
from .database import engine

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(24))

def digest(value: str): return hashlib.sha256(value.encode()).hexdigest()

def hash_password(value: str): return passwords.hash(value)

def verify(value: str, hashed: str): return passwords.verify(value, hashed)

def rate_limit(identity: str, limit=10):
    # Separate committed transaction preserves failures even when login rolls back.
    with engine.begin() as connection:
        count = connection.execute(text("""
            INSERT INTO login_buckets (key, attempts, resets_at)
            VALUES (:key, 1, now() + interval '15 minutes')
            ON CONFLICT (key) DO UPDATE SET
              attempts = CASE WHEN login_buckets.resets_at < now() THEN 1 ELSE login_buckets.attempts + 1 END,
              resets_at = CASE WHEN login_buckets.resets_at < now() THEN now() + interval '15 minutes' ELSE login_buckets.resets_at END
            RETURNING attempts
        """), {'key': digest(identity)}).scalar_one()
    if count > limit:
        raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')
