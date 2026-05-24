"""
Trader / private-wealth desk portal — liquidation funding queue for commitments.

Audience: households where the firm manages public-markets assets (e.g. Schwab) and must
liquidate positions to fund private alternative commitments. Only investors with
private_wealth=true appear in GET /trader/liquidations and related digests.

Client Associates (Account Support) are notified separately by email when
client_associate_email is set on the investor (Schwab outbound wire initiators).

Authentication: X-Trader-Key header (api_key on traders table), same pattern as advisors.

Ops provisioning (X-Firm-ID):
  POST /trader/desk              — create a desk record
  POST /trader/generate-api-key  — issue or rotate API key (returned once)

Desk self-service (X-Trader-Key):
  GET /trader/me                — profile + open ticket counts
  GET /trader/liquidations       — assigned unfunded liquidation tickets
  POST /trader/liquidations/{commitment_id}/ack — acknowledge ticket on the desk
"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.api_key_security import (
    api_key_last8,
    hash_api_key,
    require_bearer_token,
    verify_api_key,
)
from core.database import supabase
from core.trader_liquidation_digest import list_liquidation_commitments_for_trader

router = APIRouter()


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


def _resolve_trader(x_trader_key: Optional[str]) -> tuple[dict, str]:
    raw_key = (x_trader_key or "").strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-Trader-Key header required.")
    trader = (
        supabase.table("traders")
        .select("*, firms(id)")
        .eq("api_key_last8", api_key_last8(raw_key))
        .eq("is_active", True)
        .single()
        .execute()
        .data
    )
    if not trader or not verify_api_key(raw_key, trader.get("api_key_hash")):
        raise HTTPException(status_code=401, detail="Invalid or inactive trader API key.")
    firm_id = trader.get("firm_id")
    if not firm_id:
        raise HTTPException(status_code=401, detail="Trader has no firm assigned.")
    return trader, firm_id


# ---------------------------------------------------------------------------
# Ops: POST /trader/desk
# ---------------------------------------------------------------------------

class TraderDeskCreatePayload(BaseModel):
    display_name: str
    email: str


@router.post("/desk")
def create_trader_desk(
    payload: TraderDeskCreatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Create a trader desk row. Generate an API key with POST /trader/generate-api-key."""
    firm_id = _require_firm(x_firm_id)
    response = supabase.table("traders").insert(
        {
            "firm_id": firm_id,
            "display_name": payload.display_name.strip(),
            "email": payload.email.strip().lower(),
        }
    ).execute()
    row = response.data
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create trader desk.")
    return {"status": "created", "trader": row[0]}


@router.post("/generate-api-key")
def generate_trader_api_key(
    trader_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Rotate the desk API key after verifying the existing key."""
    firm_id = _require_firm(x_firm_id)
    existing_key = require_bearer_token(authorization)
    trader = (
        supabase.table("traders")
        .select("id, email, display_name, firm_id, api_key_hash")
        .eq("id", trader_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not trader:
        raise HTTPException(status_code=404, detail="Trader desk not found.")
    if not verify_api_key(existing_key, trader.get("api_key_hash")):
        raise HTTPException(status_code=401, detail="Invalid existing trader API key.")

    new_key = f"trd_{secrets.token_urlsafe(32)}"
    supabase.table("traders").update(
        {
            "api_key": new_key,
            "api_key_hash": hash_api_key(new_key),
            "api_key_last8": api_key_last8(new_key),
        }
    ).eq("id", trader_id).execute()

    return {
        "trader_id": trader_id,
        "email": trader["email"],
        "api_key": new_key,
        "message": "Store this key securely — it will not be shown again.",
    }


# ---------------------------------------------------------------------------
# GET /trader/me
# ---------------------------------------------------------------------------

@router.get("/me")
def trader_me(x_trader_key: Optional[str] = Header(default=None)):
    trader, firm_id = _resolve_trader(x_trader_key)
    open_items = list_liquidation_commitments_for_trader(firm_id, trader["id"], warn_days=None)
    return {
        "trader_id": trader["id"],
        "display_name": trader.get("display_name"),
        "email": trader.get("email"),
        "firm_id": firm_id,
        "open_liquidation_tickets": len(open_items),
    }


# ---------------------------------------------------------------------------
# GET /trader/liquidations
# ---------------------------------------------------------------------------

@router.get("/liquidations")
def trader_liquidations(x_trader_key: Optional[str] = Header(default=None)):
    """All assigned unfunded liquidation tickets (no due-date window filter)."""
    trader, firm_id = _resolve_trader(x_trader_key)
    items = list_liquidation_commitments_for_trader(firm_id, trader["id"], warn_days=None)
    return {"trader_id": trader["id"], "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# POST /trader/liquidations/{commitment_id}/ack
# ---------------------------------------------------------------------------

@router.post("/liquidations/{commitment_id}/ack")
def acknowledge_liquidation_ticket(
    commitment_id: str,
    x_trader_key: Optional[str] = Header(default=None),
):
    trader, firm_id = _resolve_trader(x_trader_key)
    c = (
        supabase.table("commitments")
        .select("id, trader_id, liquidation_required, firm_id, investors(private_wealth)")
        .eq("id", commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not c:
        raise HTTPException(status_code=404, detail="Commitment not found.")
    inv = c.get("investors") or {}
    if not inv.get("private_wealth"):
        raise HTTPException(
            status_code=400,
            detail="Liquidation desk queue is only for private-wealth (Schwab / public-markets) clients.",
        )
    if str(c.get("trader_id") or "") != str(trader["id"]):
        raise HTTPException(status_code=403, detail="This ticket is not assigned to your desk.")
    if not c.get("liquidation_required"):
        raise HTTPException(status_code=400, detail="Commitment is not flagged for liquidation.")

    now = datetime.now(timezone.utc).isoformat()
    supabase.table("commitments").update({"liquidation_acknowledged_at": now}).eq("id", commitment_id).execute()
    return {"status": "acknowledged", "commitment_id": commitment_id, "liquidation_acknowledged_at": now}
