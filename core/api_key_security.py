"""Helpers for hashing and verifying internal portal API keys."""

from __future__ import annotations

from typing import Optional

import bcrypt
from fastapi import HTTPException


def api_key_last8(raw_key: str) -> str:
    return raw_key[-8:]


def hash_api_key(raw_key: str) -> str:
    return bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()


def verify_api_key(raw_key: str, stored_hash: Optional[str]) -> bool:
    if not stored_hash:
        return False
    try:
        return bcrypt.checkpw(raw_key.encode(), stored_hash.encode())
    except ValueError:
        return False


def require_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Bearer token required to rotate API key.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Bearer token required to rotate API key.")

    return token.strip()
