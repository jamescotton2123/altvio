import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from core.database import supabase


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def mint_followup_token(
    firm_id: str,
    type: str,
    investor_id: str | None = None,
    commitment_id: str | None = None,
) -> str:
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    supabase.table("followup_tokens").insert(
        {
            "firm_id": firm_id,
            "investor_id": investor_id,
            "commitment_id": commitment_id,
            "type": type,
            "token_hash": _token_hash(raw_token),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    return raw_token


def validate_followup_token(raw_token: str) -> dict | None:
    token_hash = _token_hash(raw_token)
    rows = (
        supabase.table("followup_tokens")
        .select("*")
        .eq("token_hash", token_hash)
        .limit(1)
        .execute()
        .data
    ) or []
    row = rows[0] if rows else None
    if not row:
        return None

    now = datetime.now(timezone.utc)
    if row.get("used_at"):
        return None

    expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    if expires_at <= now:
        return None

    used_at = now.isoformat()
    (
        supabase.table("followup_tokens")
        .update({"used_at": used_at})
        .eq("id", row["id"])
        .execute()
    )
    row["used_at"] = used_at
    return row
