"""Resolve Client Associate identity from X-CA-Key (same pattern as traders / advisors)."""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from core.api_key_security import api_key_last8, verify_api_key
from core.database import supabase


def resolve_client_associate(
    x_ca_key: Optional[str],
    x_firm_id: Optional[str],
) -> tuple[dict, str]:
    """
    Returns (client_associate row, firm_id).
    Requires X-CA-Key. If X-Firm-ID is sent, it must match the associate's firm (ops belt-and-suspenders).
    """
    raw_key = (x_ca_key or "").strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-CA-Key header required for Client Associate access.")

    ca = (
        supabase.table("client_associates")
        .select("id, firm_id, email, display_name, is_active, api_key_hash")
        .eq("api_key_last8", api_key_last8(raw_key))
        .eq("is_active", True)
        .single()
        .execute()
        .data
    )
    if not ca or not verify_api_key(raw_key, ca.get("api_key_hash")):
        raise HTTPException(status_code=401, detail="Invalid or inactive Client Associate API key.")

    firm_id = ca.get("firm_id")
    if not firm_id:
        raise HTTPException(status_code=401, detail="Client Associate has no firm assigned.")

    if x_firm_id and x_firm_id.strip() != str(firm_id):
        raise HTTPException(status_code=403, detail="X-Firm-ID does not match this Client Associate key.")

    return ca, str(firm_id)
