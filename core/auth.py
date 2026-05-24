import hashlib

import bcrypt
from fastapi import HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from core.database import supabase

INTAKE_KEY_HEADER = "X-Intake-Key"


def _intake_key_rate_limit_id(request: Request) -> str:
    key = request.headers.get(INTAKE_KEY_HEADER)
    if key:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
    return get_remote_address(request)


intake_key_limiter = Limiter(key_func=_intake_key_rate_limit_id)


def resolve_firm_from_intake_key(key: str) -> str:
    if not key:
        raise HTTPException(status_code=401, detail="Invalid intake key.")

    result = (
        supabase.table("firm_intake_keys")
        .select("firm_id, key_hash, revoked_at")
        .eq("key_last8", key[-8:])
        .execute()
    )

    rows = result.data or []
    for row in rows:
        if row.get("revoked_at"):
            continue

        try:
            matches = bcrypt.checkpw(
                key.encode("utf-8"),
                row["key_hash"].encode("utf-8"),
            )
        except (KeyError, ValueError):
            matches = False

        if matches:
            return str(row["firm_id"])

    raise HTTPException(status_code=401, detail="Invalid intake key.")
