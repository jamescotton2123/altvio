"""
Investor management routes.

GET   /investors/lookup                  — find investors by email (multi-entity)
GET   /investors/households               — group investors by Orion household with AUM totals
GET   /investors/{id}                    — fetch investor record
PATCH /investors/{id}                    — update investor fields directly (ops/admin)
GET   /investors/{id}/pending-changes    — list flagged field changes awaiting ops approval
PATCH /investors/{id}/apply-change/{cid} — approve or reject a specific pending change
POST  /investors/{id}/mailings            — log physical statement / K-1 / notice mailing
GET   /investors/{id}/mailings            — list statement_mailings for investor
PATCH /investors/{id}/mailings/{mid}    — confirm receipt or update tracking / notes
GET   /investors                         — list all investors for the firm (with optional filters)
"""

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from core.database import supabase
from core.investor_lookup import lookup_investors_by_email, normalize_email

router = APIRouter()


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _get_investor(investor_id: str, firm_id: str) -> dict:
    result = (
        supabase.table("investors")
        .select("*")
        .eq("id", investor_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Investor not found.")
    return result.data


@router.get("")
def list_investors(
    x_firm_id: Optional[str] = Header(default=None),
    kyc_status: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    private_wealth: Optional[bool] = Query(default=None, description="If true, only investors flagged as private wealth (Schwab / PW book)"),
    search: Optional[str] = Query(default=None, description="Search by entity_name"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    """
    List investors for the firm. Respects advisor-scoped RLS automatically.
    Supports filtering by kyc_status, entity_type, and name search.
    """
    firm_id = _require_firm(x_firm_id)

    query = (
        supabase.table("investors")
        .select(
            "id, entity_name, entity_type, primary_email, client_one_name, advisor_email, "
            "kyc_status, orion_match_status, private_wealth, client_associate_email, "
            "schwab_estimated_liquid_cash, created_at"
        )
        .eq("firm_id", firm_id)
    )

    if kyc_status:
        query = query.eq("kyc_status", kyc_status)
    if entity_type:
        query = query.eq("entity_type", entity_type)
    if private_wealth is True:
        query = query.eq("private_wealth", True)
    elif private_wealth is False:
        query = query.eq("private_wealth", False)
    if search:
        query = query.ilike("entity_name", f"%{search}%")

    result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"investors": result.data, "count": len(result.data)}


@router.get("/lookup")
def lookup_investors(
    x_firm_id: Optional[str] = Header(default=None),
    email: str = Query(..., min_length=3, description="Email to match across contact fields"),
):
    """
    Return all investor entities associated with an email address.
    Same email may map to multiple entities (IRA, trust, LLC, etc.).
    """
    firm_id = _require_firm(x_firm_id)
    needle = normalize_email(email)
    if not needle or "@" not in needle:
        raise HTTPException(status_code=422, detail="Valid email is required.")

    matches = lookup_investors_by_email(firm_id, needle)
    return {"email": needle, "investors": matches, "count": len(matches)}


@router.get("/households")
def list_households(
    x_firm_id: Optional[str] = Header(default=None),
    search: Optional[str] = Query(default=None, description="Filter by household name"),
):
    """
    Group investors by Orion household name.

    Returns one entry per household (or one ungrouped bucket for investors
    with no orion_household_name) showing every entity that belongs to it,
    aggregate committed/funded AUM across all funds, KYC readiness, and
    the advisor relationship.

    Response shape:
    {
      "households": [
        {
          "household_name": "Blackwood Family" | null,
          "is_ungrouped": bool,
          "advisor_email": str | null,
          "entity_count": int,
          "total_committed": float,
          "total_funded": float,
          "kyc_statuses": { "Approved": 2, "Reviewing": 1 },
          "orion_match_statuses": { "Confirmed": 1, "Needs Review": 1 },
          "entities": [
            { "investor_id", "entity_name", "entity_type",
              "kyc_status", "orion_match_status", "orion_id",
              "primary_email", "advisor_email" }
          ]
        }
      ],
      "total_households": int,
      "total_ungrouped_investors": int
    }
    """
    firm_id = _require_firm(x_firm_id)

    investors_result = (
        supabase.table("investors")
        .select(
            "id, entity_name, entity_type, primary_email, advisor_email, "
            "kyc_status, orion_match_status, orion_household_name, orion_id"
        )
        .eq("firm_id", firm_id)
        .order("entity_name")
        .execute()
    )
    investors = investors_result.data or []

    # Pull commitment totals per investor in one query
    commitments_result = (
        supabase.table("commitments")
        .select("investor_id, committed_amount, funded_amount")
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
    )
    # Sum per investor_id
    totals: dict[str, dict] = {}
    for c in (commitments_result.data or []):
        inv_id = c["investor_id"]
        if inv_id not in totals:
            totals[inv_id] = {"committed": 0.0, "funded": 0.0}
        totals[inv_id]["committed"] += float(c.get("committed_amount") or 0)
        totals[inv_id]["funded"] += float(c.get("funded_amount") or 0)

    # Group by orion_household_name
    grouped: dict[str, list] = {}
    ungrouped: list = []
    for inv in investors:
        household = (inv.get("orion_household_name") or "").strip() or None
        if household:
            grouped.setdefault(household, []).append(inv)
        else:
            ungrouped.append(inv)

    def _build_entry(name: Optional[str], members: list, is_ungrouped: bool) -> dict:
        """Aggregate a list of investor records into one household entry."""
        committed = sum(totals.get(m["id"], {}).get("committed", 0.0) for m in members)
        funded = sum(totals.get(m["id"], {}).get("funded", 0.0) for m in members)

        kyc_counts: dict[str, int] = {}
        orion_counts: dict[str, int] = {}
        for m in members:
            ks = m.get("kyc_status") or "Unknown"
            kyc_counts[ks] = kyc_counts.get(ks, 0) + 1
            os_ = m.get("orion_match_status") or "Unmatched"
            orion_counts[os_] = orion_counts.get(os_, 0) + 1

        # Use the most common advisor_email across members as the household advisor
        advisor_votes: dict[str, int] = {}
        for m in members:
            ae = m.get("advisor_email")
            if ae:
                advisor_votes[ae] = advisor_votes.get(ae, 0) + 1
        advisor = max(advisor_votes, key=advisor_votes.get) if advisor_votes else None

        entities = [
            {
                "investor_id": m["id"],
                "entity_name": m.get("entity_name"),
                "entity_type": m.get("entity_type"),
                "kyc_status": m.get("kyc_status"),
                "orion_match_status": m.get("orion_match_status"),
                "orion_id": m.get("orion_id"),
                "primary_email": m.get("primary_email"),
                "advisor_email": m.get("advisor_email"),
            }
            for m in members
        ]

        return {
            "household_name": name,
            "is_ungrouped": is_ungrouped,
            "advisor_email": advisor,
            "entity_count": len(members),
            "total_committed": committed,
            "total_funded": funded,
            "kyc_statuses": kyc_counts,
            "orion_match_statuses": orion_counts,
            "entities": entities,
        }

    households = []

    # Named households, sorted alphabetically
    for name, members in sorted(grouped.items()):
        if search and search.lower() not in name.lower():
            continue
        households.append(_build_entry(name, members, is_ungrouped=False))

    # Ungrouped bucket (household_name = null)
    if ungrouped and not search:
        households.append(_build_entry(None, ungrouped, is_ungrouped=True))

    return {
        "households": households,
        "total_households": len(grouped),
        "total_ungrouped_investors": len(ungrouped),
    }


@router.get("/{investor_id}")
def get_investor(
    investor_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Fetch a single investor's full record, including fund commitments."""
    firm_id = _require_firm(x_firm_id)
    investor = _get_investor(investor_id, firm_id)
    commitments = (
        supabase.table("commitments")
        .select(
            "id, deal_id, funded_amount, committed_amount, kyc_verified, verbal_confirmed, "
            "deals(offering_name, status)"
        )
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) or []
    return {**investor, "commitments": commitments}


class InvestorUpdatePayload(BaseModel):
    entity_name: Optional[str] = None
    primary_email: Optional[str] = None
    phone: Optional[str] = None
    mailing_address: Optional[str] = None
    entity_type: Optional[str] = None
    tax_id: Optional[str] = None
    advisor_email: Optional[str] = None
    kyc_status: Optional[str] = None
    orion_id: Optional[str] = None
    orion_household_name: Optional[str] = None
    private_wealth: Optional[bool] = None
    client_associate_email: Optional[str] = None
    schwab_estimated_liquid_cash: Optional[float] = None


@router.patch("/{investor_id}")
def update_investor(
    investor_id: str,
    payload: InvestorUpdatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Directly update investor fields. Ops/admin only."""
    firm_id = _require_firm(x_firm_id)
    _get_investor(investor_id, firm_id)

    updates: dict = {}
    for k, v in payload.model_dump(exclude_unset=True).items():
        if k == "client_associate_email":
            if v is None:
                updates[k] = None
            elif isinstance(v, str):
                s = v.strip().lower()
                updates[k] = s or None
            else:
                updates[k] = v
            continue
        if k == "schwab_estimated_liquid_cash":
            updates[k] = v
            continue
        updates[k] = v

    if not updates:
        return {"status": "no_changes", "investor_id": investor_id}

    result = supabase.table("investors").update(updates).eq("id", investor_id).execute()
    if "schwab_estimated_liquid_cash" in updates:
        from core.pw_liquidation import refresh_pw_liquidation_for_investor

        refresh_pw_liquidation_for_investor(investor_id, firm_id)
    return {"status": "updated", "investor": result.data[0]}


@router.get("/{investor_id}/pending-changes")
def get_pending_changes(
    investor_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    status: Optional[str] = Query(default="Pending"),
    source: Optional[str] = Query(
        default=None,
        description="Filter by source, e.g. subdoc_extraction, kyc_extraction, loi_sync",
    ),
):
    """
    Return pending field changes for an investor (LOI sync, KYC, sub-doc extraction).
    Filtered to Pending status by default — pass ?status=all for full history.
    """
    firm_id = _require_firm(x_firm_id)
    _get_investor(investor_id, firm_id)

    query = (
        supabase.table("investor_pending_changes")
        .select("*")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
    )

    if status and status.lower() != "all":
        query = query.eq("status", status)
    if source:
        query = query.eq("source", source)

    result = query.execute()
    return {"investor_id": investor_id, "pending_changes": result.data}


class ApplyChangePayload(BaseModel):
    approved: bool
    reviewed_by: Optional[str] = None


@router.patch("/{investor_id}/apply-change/{change_id}")
def apply_change(
    investor_id: str,
    change_id: str,
    payload: ApplyChangePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Approve or reject a flagged investor field change.
    If approved, the proposed value is applied to the investors record immediately.
    Approval is logged via the audit_log trigger automatically.
    """
    firm_id = _require_firm(x_firm_id)
    _get_investor(investor_id, firm_id)

    from core.loi_data_sync import apply_pending_change

    result = apply_pending_change(
        change_id=change_id,
        investor_id=investor_id,
        firm_id=firm_id,
        approved=payload.approved,
        reviewed_by=payload.reviewed_by or "ops",
    )
    return result


# ---------------------------------------------------------------------------
# Physical / statement mailings (statement_mailings)
# ---------------------------------------------------------------------------


class StatementMailingCreatePayload(BaseModel):
    document_type: str
    period: str
    mailed_date: Optional[date] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{investor_id}/mailings")
def create_statement_mailing(
    investor_id: str,
    payload: StatementMailingCreatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Insert a statement_mailings row; firm_id is taken from the investor record."""
    firm_id = _require_firm(x_firm_id)
    inv = _get_investor(investor_id, firm_id)

    row = {
        "firm_id": inv["firm_id"],
        "investor_id": investor_id,
        "document_type": payload.document_type,
        "period": payload.period,
        "mailed_date": payload.mailed_date.isoformat() if payload.mailed_date else None,
        "tracking_number": payload.tracking_number,
        "notes": payload.notes,
    }
    result = supabase.table("statement_mailings").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Insert failed.")
    return result.data[0]


@router.get("/{investor_id}/mailings")
def list_statement_mailings(
    investor_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """All statement_mailings for this investor, newest first."""
    firm_id = _require_firm(x_firm_id)
    _get_investor(investor_id, firm_id)

    result = (
        supabase.table("statement_mailings")
        .select("*")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"investor_id": investor_id, "mailings": result.data or []}


class StatementMailingPatchPayload(BaseModel):
    confirmed_received: Optional[bool] = None
    confirmed_at: Optional[datetime] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/{investor_id}/mailings/{mailing_id}")
def patch_statement_mailing(
    investor_id: str,
    mailing_id: str,
    payload: StatementMailingPatchPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Update a mailing row (confirm receipt, tracking, notes)."""
    firm_id = _require_firm(x_firm_id)
    _get_investor(investor_id, firm_id)

    chk = (
        supabase.table("statement_mailings")
        .select("*")
        .eq("id", mailing_id)
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .limit(1)
        .execute()
    )
    rows = chk.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Mailing not found.")
    existing_row = rows[0]

    dump = payload.model_dump(exclude_unset=True)
    updates: dict = {}
    if "confirmed_received" in dump:
        updates["confirmed_received"] = dump["confirmed_received"]
    if "tracking_number" in dump:
        updates["tracking_number"] = dump["tracking_number"]
    if "notes" in dump:
        updates["notes"] = dump["notes"]
    if "confirmed_at" in dump:
        ca = dump["confirmed_at"]
        updates["confirmed_at"] = ca.isoformat() if ca is not None else None
    elif dump.get("confirmed_received") is True:
        updates["confirmed_at"] = datetime.now(timezone.utc).isoformat()

    if not updates:
        return existing_row

    result = (
        supabase.table("statement_mailings")
        .update(updates)
        .eq("id", mailing_id)
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Mailing not found.")
    return result.data[0]
