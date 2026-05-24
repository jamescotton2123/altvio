"""
Tamper-evident audit ledger utilities.
"""

import hashlib
import json
from typing import Any

from core.database import supabase


def _canonical_json_value(value: Any) -> Any:
    """Keep hash inputs stable when callers pass UUIDs, Decimals, or datetimes."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _canonical_json_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_canonical_json_value(v) for v in value]
        if isinstance(value, tuple):
            return [_canonical_json_value(v) for v in value]
        return str(value)


def build_audit_payload(
    firm_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    metadata: dict | None = None,
    prior_hash: str = "",
) -> dict:
    return {
        "firm_id": str(firm_id),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "before": _canonical_json_value(before),
        "after": _canonical_json_value(after),
        "metadata": _canonical_json_value(metadata),
        "prior_hash": prior_hash or "",
    }


def compute_row_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def log_audit(
    firm_id,
    actor_type,
    actor_id,
    action,
    entity_type,
    entity_id=None,
    before=None,
    after=None,
    metadata=None,
):
    prior = (
        supabase.table("audit_logs")
        .select("row_hash")
        .eq("firm_id", str(firm_id))
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    prior_hash = prior.data[0]["row_hash"] if prior.data else ""
    payload = build_audit_payload(
        firm_id=firm_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        metadata=metadata,
        prior_hash=prior_hash,
    )
    row_hash = compute_row_hash(payload)
    inserted = (
        supabase.table("audit_logs")
        .insert({**payload, "row_hash": row_hash})
        .execute()
    )
    return inserted.data[0]["id"]


def verify_audit_chain(firm_id: str) -> dict:
    rows = (
        supabase.table("audit_logs")
        .select("*")
        .eq("firm_id", str(firm_id))
        .order("id")
        .execute()
    ).data

    prior_hash = ""
    for row in rows:
        if row.get("prior_hash", "") != prior_hash:
            return {"ok": False, "first_bad_row_id": row["id"]}

        payload = build_audit_payload(
            firm_id=row["firm_id"],
            actor_type=row["actor_type"],
            actor_id=row.get("actor_id"),
            action=row["action"],
            entity_type=row["entity_type"],
            entity_id=row.get("entity_id"),
            before=row.get("before"),
            after=row.get("after"),
            metadata=row.get("metadata"),
            prior_hash=row.get("prior_hash", ""),
        )
        expected_hash = compute_row_hash(payload)
        if row.get("row_hash") != expected_hash:
            return {"ok": False, "first_bad_row_id": row["id"]}
        prior_hash = row["row_hash"]

    return {"ok": True, "rows_checked": len(rows)}
