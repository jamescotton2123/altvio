"""
Advisor Portal — role-scoped dashboard for individual advisors.

Advisors see ONLY their clients. They cannot see other advisors' books,
firm-level metrics, or any investor not linked to their advisor_id.

Authentication: X-Advisor-Key header (api_key on advisors table).
Fallback for ops/admin access: X-Firm-ID header bypasses advisor scoping.

Key views:
  GET /advisor/me              — advisor profile + summary stats
  GET /advisor/clients         — all their investors with current status
  GET /advisor/clients/{id}    — single client deep-dive
  GET /advisor/activity        — recent events across their book
  POST /advisor/intake         — self-service intake for a new client
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
from core.commitment_status import SIGNED_STATES
from core.database import supabase

router = APIRouter()

DEFAULT_ADVISOR_PREFS = {
    "client_sort": "committed_desc",   # committed_desc | name_asc | kyc_status | overall_status
    "default_filter": "all",           # all | kyc_pending | subdocs_out | awaiting_funding | funded | missing_wire
    "show_dollar_amounts": True,
    "show_columns": ["entity_name", "kyc_status", "total_committed", "overall_status", "wire_on_file", "fund_count"],
}

VALID_COLUMNS = ["entity_name", "entity_type", "primary_email", "phone", "kyc_status",
                 "total_committed", "total_funded", "overall_status", "wire_on_file",
                 "fund_count", "handle_with_care", "sharepoint_link"]

VALID_SORTS = ["committed_desc", "name_asc", "kyc_status", "overall_status", "funded_desc"]
VALID_FILTERS = ["all", "kyc_pending", "subdocs_out", "awaiting_funding", "funded", "missing_wire"]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _resolve_advisor(
    x_advisor_key: Optional[str],
    x_firm_id: Optional[str],
    advisor_email: Optional[str] = None,
) -> tuple[dict, str]:
    """
    Returns (advisor_record, firm_id).
    Resolves by API key (advisor self-service) or firm_id + email (ops/admin).
    """
    if x_advisor_key:
        raw_key = x_advisor_key.strip()
        advisor = (
            supabase.table("advisors")
            .select("*, firms(id)")
            .eq("api_key_last8", api_key_last8(raw_key))
            .eq("is_active", True)
            .single()
            .execute()
            .data
        )
        if not advisor or not verify_api_key(raw_key, advisor.get("api_key_hash")):
            raise HTTPException(status_code=401, detail="Invalid or inactive advisor API key.")
        firm_id = advisor.get("firm_id")
        if not firm_id:
            raise HTTPException(status_code=401, detail="Advisor has no firm assigned.")
        return advisor, firm_id

    if x_firm_id and advisor_email:
        advisor = (
            supabase.table("advisors")
            .select("*")
            .eq("firm_id", x_firm_id)
            .eq("email", advisor_email.strip().lower())
            .eq("is_active", True)
            .single()
            .execute()
            .data
        )
        if not advisor:
            raise HTTPException(status_code=404, detail="Advisor not found.")
        return advisor, x_firm_id

    raise HTTPException(
        status_code=401,
        detail="Provide X-Advisor-Key header or X-Firm-ID + X-Advisor-Email headers.",
    )


# ---------------------------------------------------------------------------
# Preferences helpers
# ---------------------------------------------------------------------------

def _get_prefs(advisor: dict) -> dict:
    saved = advisor.get("preferences") or {}
    return {**DEFAULT_ADVISOR_PREFS, **saved}


# ---------------------------------------------------------------------------
# GET/PATCH /advisor/preferences
# ---------------------------------------------------------------------------

class AdvisorPrefsPayload(BaseModel):
    client_sort: Optional[str] = None
    default_filter: Optional[str] = None
    show_dollar_amounts: Optional[bool] = None
    show_columns: Optional[list[str]] = None


@router.get("/preferences")
def get_advisor_preferences(
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """Return current advisor preferences with available options."""
    advisor, _ = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    return {
        "preferences": _get_prefs(advisor),
        "available": {
            "sort_options": VALID_SORTS,
            "filter_options": VALID_FILTERS,
            "column_options": VALID_COLUMNS,
        },
    }


@router.patch("/preferences")
def update_advisor_preferences(
    payload: AdvisorPrefsPayload,
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """Update advisor's personal dashboard preferences. Partial updates supported."""
    advisor, _ = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    current = _get_prefs(advisor)

    if payload.client_sort is not None:
        if payload.client_sort not in VALID_SORTS:
            raise HTTPException(status_code=400, detail=f"Invalid sort. Valid: {VALID_SORTS}")
        current["client_sort"] = payload.client_sort
    if payload.default_filter is not None:
        if payload.default_filter not in VALID_FILTERS:
            raise HTTPException(status_code=400, detail=f"Invalid filter. Valid: {VALID_FILTERS}")
        current["default_filter"] = payload.default_filter
    if payload.show_dollar_amounts is not None:
        current["show_dollar_amounts"] = payload.show_dollar_amounts
    if payload.show_columns is not None:
        invalid = [c for c in payload.show_columns if c not in VALID_COLUMNS]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid columns: {invalid}. Valid: {VALID_COLUMNS}")
        current["show_columns"] = payload.show_columns

    supabase.table("advisors").update({"preferences": current}).eq("id", advisor["id"]).execute()
    return {"status": "updated", "preferences": current}


# ---------------------------------------------------------------------------
# GET /advisor/me
# ---------------------------------------------------------------------------

@router.get("/me")
def get_advisor_profile(
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """Advisor profile + high-level book summary."""
    advisor, firm_id = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    advisor_id = advisor["id"]

    investors = (
        supabase.table("investors")
        .select("id, entity_name, kyc_status, wire_instructions, handle_with_care")
        .eq("firm_id", firm_id)
        .eq("advisor_id", advisor_id)
        .execute()
        .data
    ) or []

    commitments = (
        supabase.table("commitments")
        .select("committed_amount, funded_amount, docusign_status, wire_status, investors(advisor_id)")
        .eq("firm_id", firm_id)
        .execute()
        .data
    ) or []

    # Filter to this advisor's commitments via investors
    my_investor_ids = {inv["id"] for inv in investors}
    my_commitments = [
        c for c in commitments
        if (c.get("investors") or {}).get("advisor_id") == advisor_id
    ]

    total_committed = sum(float(c.get("committed_amount") or 0) for c in my_commitments)
    total_funded = sum(float(c.get("funded_amount") or 0) for c in my_commitments)

    pipeline = {
        "kyc_pending": sum(1 for inv in investors if inv.get("kyc_status") not in ("Approved",)),
        "subdocs_out": sum(1 for c in my_commitments if c.get("docusign_status") == "Sent"),
        "awaiting_funding": sum(
            1 for c in my_commitments
            if c.get("docusign_status") in SIGNED_STATES and float(c.get("funded_amount") or 0) == 0
        ),
        "funded": sum(1 for c in my_commitments if float(c.get("funded_amount") or 0) > 0),
        "missing_wire": sum(
            1 for inv in investors
            if not inv.get("wire_instructions") and inv["id"] in my_investor_ids
        ),
    }

    return {
        "advisor": {
            "id": advisor_id,
            "name": f"{advisor.get('first_name', '')} {advisor.get('last_name', '')}".strip(),
            "email": advisor.get("email"),
            "title": advisor.get("title"),
            "rep_code": advisor.get("rep_code"),
        },
        "book_summary": {
            "client_count": len(investors),
            "total_committed": total_committed,
            "total_funded": total_funded,
            "funding_rate_pct": round(total_funded / total_committed * 100, 1) if total_committed else 0,
        },
        "pipeline": pipeline,
    }


# ---------------------------------------------------------------------------
# GET /advisor/clients
# ---------------------------------------------------------------------------

@router.get("/clients")
def get_advisor_clients(
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """
    Full client list for this advisor with current pipeline status.
    Each investor is shown with their latest commitment status across all deals.
    """
    advisor, firm_id = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    advisor_id = advisor["id"]
    prefs = _get_prefs(advisor)
    show_dollars = prefs.get("show_dollar_amounts", True)
    visible_cols = set(prefs.get("show_columns", DEFAULT_ADVISOR_PREFS["show_columns"]))
    active_filter = prefs.get("default_filter", "all")
    sort_by = prefs.get("client_sort", "committed_desc")

    investors = (
        supabase.table("investors")
        .select(
            "id, entity_name, entity_type, primary_email, phone, "
            "kyc_status, wire_instructions, handle_with_care, sensitivity_notes, "
            "sharepoint_link, created_at"
        )
        .eq("firm_id", firm_id)
        .eq("advisor_id", advisor_id)
        .order("entity_name")
        .execute()
        .data
    ) or []

    if not investors:
        return {"advisor_id": advisor_id, "clients": []}

    investor_ids = [inv["id"] for inv in investors]

    # Fetch all commitments for these investors in one query
    commitments = (
        supabase.table("commitments")
        .select(
            "id, investor_id, committed_amount, funded_amount, "
            "docusign_status, wire_status, loi_status, created_at, commitment_date, "
            "deals(offering_name, status, close_date)"
        )
        .in_("investor_id", investor_ids)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
        .data
    ) or []

    # Group commitments by investor
    commitments_by_investor: dict[str, list] = {}
    for c in commitments:
        inv_id = c["investor_id"]
        commitments_by_investor.setdefault(inv_id, []).append(c)

    clients = []
    for inv in investors:
        inv_id = inv["id"]
        inv_commitments = commitments_by_investor.get(inv_id, [])

        total_committed = sum(float(c.get("committed_amount") or 0) for c in inv_commitments)
        total_funded = sum(float(c.get("funded_amount") or 0) for c in inv_commitments)

        # Determine overall status from most recent commitment
        latest = inv_commitments[0] if inv_commitments else None
        if latest:
            if float(latest.get("funded_amount") or 0) > 0:
                overall_status = "Funded"
            elif latest.get("docusign_status") in SIGNED_STATES:
                overall_status = "Signed — Awaiting Wire"
            elif latest.get("docusign_status") == "Sent":
                overall_status = "Sub Docs Out"
            elif inv.get("kyc_status") == "Approved":
                overall_status = "KYC Approved"
            else:
                overall_status = f"KYC {inv.get('kyc_status', 'Pending')}"
        else:
            overall_status = "No Commitments"

        clients.append({
            "investor_id": inv_id,
            "entity_name": inv.get("entity_name"),
            "entity_type": inv.get("entity_type"),
            "primary_email": inv.get("primary_email"),
            "phone": inv.get("phone"),
            "kyc_status": inv.get("kyc_status"),
            "handle_with_care": inv.get("handle_with_care", False),
            "sensitivity_notes": inv.get("sensitivity_notes"),
            "has_wire_on_file": bool(inv.get("wire_instructions")),
            "sharepoint_link": inv.get("sharepoint_link"),
            "overall_status": overall_status,
            "total_committed": total_committed,
            "total_funded": total_funded,
            "fund_count": len(inv_commitments),
            "commitments": [
                {
                    "commitment_id": c["id"],
                    "offering_name": (c.get("deals") or {}).get("offering_name", ""),
                    "deal_status": (c.get("deals") or {}).get("status", ""),
                    "committed_amount": float(c.get("committed_amount") or 0),
                    "funded_amount": float(c.get("funded_amount") or 0),
                    "docusign_status": c.get("docusign_status"),
                    "wire_status": c.get("wire_status"),
                    "commitment_date": c.get("commitment_date"),
                }
                for c in inv_commitments
            ],
        })

    # Apply filter preference
    filter_map = {
        "kyc_pending":       lambda c: c["kyc_status"] not in ("Approved",),
        "subdocs_out":       lambda c: any(cm["docusign_status"] == "Sent" for cm in c["commitments"]),
        "awaiting_funding":  lambda c: any(cm["docusign_status"] in SIGNED_STATES and cm["funded_amount"] == 0 for cm in c["commitments"]),
        "funded":            lambda c: c["total_funded"] > 0,
        "missing_wire":      lambda c: not c["has_wire_on_file"],
    }
    if active_filter in filter_map:
        clients = [c for c in clients if filter_map[active_filter](c)]

    # Apply sort preference
    sort_fns = {
        "committed_desc":  lambda c: (-int(c["handle_with_care"]), -c["total_committed"]),
        "name_asc":        lambda c: c["entity_name"].lower(),
        "kyc_status":      lambda c: (c["kyc_status"] or ""),
        "overall_status":  lambda c: (c["overall_status"] or ""),
        "funded_desc":     lambda c: (-int(c["handle_with_care"]), -c["total_funded"]),
    }
    clients.sort(key=sort_fns.get(sort_by, sort_fns["committed_desc"]))

    # Strip dollar amounts if preference is off
    if not show_dollars:
        for c in clients:
            c["total_committed"] = None
            c["total_funded"] = None
            for cm in c.get("commitments", []):
                cm["committed_amount"] = None
                cm["funded_amount"] = None

    # Strip columns not in visible_cols (always keep entity_name and investor_id)
    always_keep = {"investor_id", "entity_name", "handle_with_care", "commitments"}
    for c in clients:
        to_remove = [k for k in list(c.keys()) if k not in always_keep and k not in visible_cols]
        for k in to_remove:
            del c[k]

    return {
        "advisor_id": advisor_id,
        "client_count": len(clients),
        "active_filter": active_filter,
        "active_sort": sort_by,
        "clients": clients,
    }


# ---------------------------------------------------------------------------
# GET /advisor/clients/{investor_id}
# ---------------------------------------------------------------------------

@router.get("/clients/{investor_id}")
def get_advisor_client(
    investor_id: str,
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """Single client deep-dive. Verifies the client belongs to this advisor."""
    advisor, firm_id = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    advisor_id = advisor["id"]

    investor = (
        supabase.table("investors")
        .select("*")
        .eq("id", investor_id)
        .eq("firm_id", firm_id)
        .eq("advisor_id", advisor_id)
        .single()
        .execute()
        .data
    )
    if not investor:
        raise HTTPException(status_code=404, detail="Client not found or not assigned to you.")

    commitments = (
        supabase.table("commitments")
        .select("*, deals(offering_name, fund_manager, target_raise, close_date, status)")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
        .data
    ) or []

    distributions = (
        supabase.table("distribution_notices")
        .select("individual_amount, status, distributions(distribution_date, distribution_type)")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Sent")
        .execute()
        .data
    ) or []

    pending_changes = (
        supabase.table("investor_pending_changes")
        .select("field_name, status, created_at")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Pending")
        .execute()
        .data
    ) or []

    # Strip sensitive wire details from advisor view — they see status only
    investor_safe = {k: v for k, v in investor.items() if k != "wire_instructions"}
    investor_safe["has_wire_on_file"] = bool(investor.get("wire_instructions"))

    return {
        "investor": investor_safe,
        "commitments": commitments,
        "distribution_history": distributions,
        "pending_changes": pending_changes,
    }


# ---------------------------------------------------------------------------
# GET /advisor/activity
# ---------------------------------------------------------------------------

@router.get("/activity")
def get_advisor_activity(
    x_advisor_key: Optional[str] = Header(default=None),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """Recent events across all of this advisor's clients."""
    advisor, firm_id = _resolve_advisor(x_advisor_key, x_firm_id, x_advisor_email)
    advisor_id = advisor["id"]

    investor_ids = [
        inv["id"]
        for inv in (
            supabase.table("investors")
            .select("id")
            .eq("firm_id", firm_id)
            .eq("advisor_id", advisor_id)
            .execute()
            .data or []
        )
    ]
    if not investor_ids:
        return {"advisor_id": advisor_id, "activity": []}

    commitment_ids = [
        c["id"]
        for c in (
            supabase.table("commitments")
            .select("id")
            .in_("investor_id", investor_ids)
            .eq("firm_id", firm_id)
            .execute()
            .data or []
        )
    ]
    if not commitment_ids:
        return {"advisor_id": advisor_id, "activity": []}

    events = (
        supabase.table("commitment_events")
        .select("event_type, changed_at, new_value, investors(entity_name), deals(offering_name)")
        .in_("commitment_id", commitment_ids)
        .order("changed_at", desc=True)
        .limit(30)
        .execute()
        .data
    ) or []

    return {
        "advisor_id": advisor_id,
        "activity": [
            {
                "event_type": e.get("event_type"),
                "entity_name": (e.get("investors") or {}).get("entity_name", ""),
                "offering_name": (e.get("deals") or {}).get("offering_name", ""),
                "value": e.get("new_value"),
                "occurred_at": e.get("changed_at"),
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# POST /advisor/generate-api-key  (ops-only, creates/rotates advisor key)
# ---------------------------------------------------------------------------

@router.post("/generate-api-key")
def generate_advisor_api_key(
    advisor_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """
    Rotate an API key for an advisor after verifying the existing key.
    The key is returned once — store it securely.
    """
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    existing_key = require_bearer_token(authorization)

    advisor = (
        supabase.table("advisors")
        .select("id, email, firm_id, api_key_hash")
        .eq("id", advisor_id)
        .eq("firm_id", x_firm_id)
        .single()
        .execute()
        .data
    )
    if not advisor:
        raise HTTPException(status_code=404, detail="Advisor not found.")
    if not verify_api_key(existing_key, advisor.get("api_key_hash")):
        raise HTTPException(status_code=401, detail="Invalid existing advisor API key.")

    new_key = f"adv_{secrets.token_urlsafe(32)}"
    supabase.table("advisors").update(
        {
            "api_key": new_key,
            "api_key_hash": hash_api_key(new_key),
            "api_key_last8": api_key_last8(new_key),
        }
    ).eq("id", advisor_id).execute()

    return {
        "advisor_id": advisor_id,
        "email": advisor["email"],
        "api_key": new_key,
        "message": "Store this key securely — it will not be shown again.",
    }


