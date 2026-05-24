"""
Client Associate portal — API-key auth for private-wealth / Schwab funding views.

Ops (X-Firm-ID):
  POST /client-associate/desk              — register a CA row (email must match investors.client_associate_email)
  POST /client-associate/generate-api-key — issue or rotate API key (returned once)

Self-service (X-CA-Key):
  GET /client-associate/me                  — profile
  GET /client-associate/pw-deals          — active deals where this CA has PW commitments on their book

Deal drill-in uses GET /deals/{deal_id}/hub?role=client_associate with X-CA-Key
(optional X-Firm-ID must match the key's firm).
"""

import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.api_key_security import (
    api_key_last8,
    hash_api_key,
    require_bearer_token,
    verify_api_key,
)
from core.client_associate_auth import resolve_client_associate
from core.database import supabase

router = APIRouter()


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


class ClientAssociateDeskCreatePayload(BaseModel):
    email: str
    display_name: str = ""


@router.post("/desk")
def create_client_associate_desk(
    payload: ClientAssociateDeskCreatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Create a Client Associate row. Issue an API key with POST /client-associate/generate-api-key."""
    firm_id = _require_firm(x_firm_id)
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email is required.")

    existing = (
        supabase.table("client_associates")
        .select("id")
        .eq("firm_id", firm_id)
        .eq("email", email)
        .limit(1)
        .execute()
        .data
    )
    if existing:
        raise HTTPException(status_code=409, detail="A Client Associate with this email already exists for the firm.")

    response = supabase.table("client_associates").insert(
        {
            "firm_id": firm_id,
            "email": email,
            "display_name": (payload.display_name or "").strip(),
        }
    ).execute()
    row = response.data
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create Client Associate desk.")
    return {"status": "created", "client_associate": row[0]}


@router.post("/generate-api-key")
def generate_client_associate_api_key(
    client_associate_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Rotate the CA API key after verifying the existing key."""
    firm_id = _require_firm(x_firm_id)
    existing_key = require_bearer_token(authorization)
    ca = (
        supabase.table("client_associates")
        .select("id, email, display_name, firm_id, api_key_hash")
        .eq("id", client_associate_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not ca:
        raise HTTPException(status_code=404, detail="Client Associate desk not found.")
    if not verify_api_key(existing_key, ca.get("api_key_hash")):
        raise HTTPException(status_code=401, detail="Invalid existing Client Associate API key.")

    new_key = f"ca_{secrets.token_urlsafe(32)}"
    supabase.table("client_associates").update(
        {
            "api_key": new_key,
            "api_key_hash": hash_api_key(new_key),
            "api_key_last8": api_key_last8(new_key),
        }
    ).eq("id", client_associate_id).execute()

    return {
        "client_associate_id": client_associate_id,
        "email": ca["email"],
        "api_key": new_key,
        "message": "Store this key securely — it will not be shown again.",
    }


@router.get("/me")
def client_associate_me(
    x_ca_key: Optional[str] = Header(default=None, alias="X-CA-Key"),
    x_firm_id: Optional[str] = Header(default=None),
):
    ca, _firm_id = resolve_client_associate(x_ca_key, x_firm_id)
    return {
        "id": ca["id"],
        "email": ca["email"],
        "display_name": ca.get("display_name") or "",
    }


@router.get("/pw-deals")
def list_client_associate_pw_deals(
    x_ca_key: Optional[str] = Header(default=None, alias="X-CA-Key"),
    x_firm_id: Optional[str] = Header(default=None),
):
    """Active deals where this CA has at least one Active PW commitment on their book (by email match)."""
    ca, firm_id = resolve_client_associate(x_ca_key, x_firm_id)
    ca_email = (ca.get("email") or "").strip().lower()

    raw = (
        supabase.table("commitments")
        .select(
            "deal_id, deals(id, offering_name, status), "
            "investors(private_wealth, client_associate_email)"
        )
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) or []

    by_deal: dict[str, dict] = {}
    for row in raw:
        inv = row.get("investors") or {}
        if not inv.get("private_wealth"):
            continue
        if (inv.get("client_associate_email") or "").strip().lower() != ca_email:
            continue
        d = row.get("deals") or {}
        if not d.get("id"):
            continue
        if d.get("status") != "Active":
            continue
        did = str(d["id"])
        if did not in by_deal:
            by_deal[did] = {
                "deal_id": did,
                "offering_name": d.get("offering_name"),
                "pw_commitment_count": 0,
            }
        by_deal[did]["pw_commitment_count"] += 1

    deals = sorted(by_deal.values(), key=lambda x: (x.get("offering_name") or "").lower())
    return {"deals": deals, "count": len(deals)}
