"""
Deal Hub routes — the central ops dashboard for active and closed deals.

Active Deal Dashboard (deals.status = 'Active'):
  GET  /deals/active                       — list active deals (role-scoped)
  GET  /deals/{deal_id}/hub                — drill-in view (role=ops|ceo|advisor|client_associate; CA uses X-CA-Key; CEO may set include_investors=true for ops-level rows)
  POST /deals/{deal_id}/hub/request-missing-info — targeted KYC/wire outreach
  POST /deals/{deal_id}/hub/mass-email     — filtered bulk email
  GET  /deals/{deal_id}/hub/wire-instructions — pull all outbound wire details

Fund Ledger (deals.status = 'Closed' | 'Dissolved'):
  GET  /deals/ledger                       — list closed/dissolved deals
  GET  /deals/{deal_id}/ledger             — drill-in ledger view

Deal lifecycle:
  GET  /deals/{deal_id}/readiness        — pre-close checklist (gaps per investor)
  POST /deals                              — create a new deal (optional nested fee_arrangement)
  POST /deals/{deal_id}/close             — close deal + auto-trigger Orion NAImport
  POST /deals/distributions               — initiate a distribution
  POST /deals/{deal_id}/dissolve          — dissolve + final AIP export

Third-party fee arrangements:
  POST /deals/{deal_id}/fee-arrangements         — optional fields: only describe what applies;
     upfront % of commitment, flat upfront (per investor or pro-rata deal total), implementation
     (optional equal split across active commitments when include_implementation_in_wire), carry (
     disclosure only for wire). Expiry alerts only when an upfront fee exists.
  GET  /deals/{deal_id}/fee-arrangements         — list all fee arrangements for a deal
  PATCH /deals/{deal_id}/fee-arrangements/{id}   — update arrangement
  DELETE /deals/{deal_id}/fee-arrangements/{id} — remove arrangement
  GET  /deals/fee-arrangements/expiring          — firm-wide expiring upfront windows
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from core.client_associate_auth import resolve_client_associate
from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.deal_fees import UPFRONT_AMOUNT_BASIS
from core.deal_readiness import check_deal_readiness
from core.funding_source import funding_source_hub_flags
from core.pw_liquidation import refresh_pw_liquidation_for_deal

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _format_wire_instructions(wi: dict | str | None) -> str:
    """
    Render wire instructions as clean plain text for email bodies.
    Accepts the structured JSONB dict or a legacy plain-text string.
    """
    if not wi:
        return "Wire instructions not yet configured. Contact operations."
    if isinstance(wi, str):
        return wi
    lines = []
    if wi.get("bank_name"):
        lines.append(f"Bank Name:       {wi['bank_name']}")
    if wi.get("bank_address"):
        lines.append(f"Bank Address:    {wi['bank_address']}")
    if wi.get("routing_number"):
        lines.append(f"ABA/Routing:     {wi['routing_number']}")
    if wi.get("swift_code"):
        lines.append(f"SWIFT/BIC:       {wi['swift_code']}")
    if wi.get("iban"):
        lines.append(f"IBAN:            {wi['iban']}")
    if wi.get("account_name"):
        lines.append(f"Account Name:    {wi['account_name']}")
    if wi.get("account_number"):
        lines.append(f"Account Number:  {wi['account_number']}")
    if wi.get("further_credit"):
        lines.append(f"FFC:             {wi['further_credit']}")
    if wi.get("reference"):
        lines.append(f"Reference/Memo:  {wi['reference']}")
    if wi.get("notes"):
        lines.append(f"\nNote: {wi['notes']}")
    return "\n".join(lines) if lines else "Wire instructions on file — contact operations."


def _get_firm_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


_notice_commitment_column: Optional[bool] = None


def _distribution_notice_has_commitment_id() -> bool:
    """True after migration 20260543_distribution_notices_commitment_id is applied."""
    global _notice_commitment_column
    if _notice_commitment_column is None:
        try:
            supabase.table("distribution_notices").select("commitment_id").limit(1).execute()
            _notice_commitment_column = True
        except Exception:
            _notice_commitment_column = False
    return _notice_commitment_column


def _distribution_notice_insert_row(
    *,
    firm_id: str,
    distribution_id: str,
    investor_id: str,
    individual_amount: float,
    commitment_id: Optional[str] = None,
    kyc_verified: bool = False,
    status: str = "Pending",
) -> dict:
    row = {
        "firm_id": firm_id,
        "distribution_id": distribution_id,
        "investor_id": investor_id,
        "individual_amount": individual_amount,
        "status": status,
    }
    if kyc_verified:
        row["kyc_verified"] = kyc_verified
    if _distribution_notice_has_commitment_id() and commitment_id:
        row["commitment_id"] = commitment_id
    return row


# ---------------------------------------------------------------------------
# Third-party fee arrangements (placement agent, sub-advisor, etc.)
# ---------------------------------------------------------------------------

ARRANGEMENT_TYPES = ("placement_agent", "sub_advisor", "referral_partner", "other")
EXPIRY_ALERT_DAYS = 90   # warn this many days before upfront fee term ends


class FeeArrangementPayload(BaseModel):
    """All economics fields optional except where noted; describe only the structure the deal uses."""
    recipient_name: Optional[str] = None
    recipient_email: Optional[str] = None
    arrangement_type: str = "placement_agent"
    implementation_fee: Optional[float] = None
    upfront_fee_pct: Optional[float] = None
    upfront_fee_amount: Optional[float] = None
    upfront_fee_amount_basis: str = "per_commitment"  # per_commitment | pro_rata_deal_total
    include_implementation_in_wire: bool = False
    upfront_fee_term_years: int = 3
    upfront_fee_start_date: Optional[date] = None
    carry_pct: Optional[float] = None
    carry_hurdle_pct: Optional[float] = None
    notes: Optional[str] = None


class FeeArrangementUpdatePayload(BaseModel):
    recipient_name: Optional[str] = None
    recipient_email: Optional[str] = None
    arrangement_type: Optional[str] = None
    implementation_fee: Optional[float] = None
    upfront_fee_pct: Optional[float] = None
    upfront_fee_amount: Optional[float] = None
    upfront_fee_amount_basis: Optional[str] = None
    include_implementation_in_wire: Optional[bool] = None
    upfront_fee_term_years: Optional[int] = None
    upfront_fee_start_date: Optional[date] = None
    upfront_fee_expiry_date: Optional[date] = None
    carry_pct: Optional[float] = None
    carry_hurdle_pct: Optional[float] = None
    notes: Optional[str] = None


def _compute_expiry(start: date | None, term_years: int) -> date | None:
    if not start:
        return None
    try:
        return start.replace(year=start.year + term_years)
    except ValueError:
        return start.replace(year=start.year + term_years, day=28)


def _expiry_status(expiry: date | None, today: date) -> dict:
    if not expiry:
        return {"expiry_date": None, "days_remaining": None, "alert": None}
    days = (expiry - today).days
    if days < 0:
        alert = "expired"
    elif days <= EXPIRY_ALERT_DAYS:
        alert = "expiring_soon"
    else:
        alert = None
    return {"expiry_date": expiry.isoformat(), "days_remaining": days, "alert": alert}


def _economics_present(p: FeeArrangementPayload) -> bool:
    return any(
        x is not None
        for x in (
            p.implementation_fee,
            p.upfront_fee_pct,
            p.upfront_fee_amount,
            p.carry_pct,
            p.carry_hurdle_pct,
        )
    )


def _validate_fee_arrangement_payload(payload: FeeArrangementPayload) -> None:
    basis = (payload.upfront_fee_amount_basis or "per_commitment").strip()
    if basis not in UPFRONT_AMOUNT_BASIS:
        raise HTTPException(
            status_code=400,
            detail=f"upfront_fee_amount_basis must be one of: {sorted(UPFRONT_AMOUNT_BASIS)}",
        )
    if payload.include_implementation_in_wire and payload.implementation_fee is None:
        raise HTTPException(
            status_code=400,
            detail="include_implementation_in_wire requires implementation_fee.",
        )
    if not _economics_present(payload) and not (payload.recipient_name or "").strip() and not (payload.notes or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Provide at least one fee or carry field, or enter recipient_name / notes for manual tracking.",
        )


def _insert_fee_arrangement(
    firm_id: str,
    deal_id: str,
    deal_offering_name: str,
    payload: FeeArrangementPayload,
) -> dict:
    if payload.arrangement_type not in ARRANGEMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"arrangement_type must be one of: {ARRANGEMENT_TYPES}")
    _validate_fee_arrangement_payload(payload)

    has_upfront = payload.upfront_fee_pct is not None or payload.upfront_fee_amount is not None
    start: date | None
    expiry: date | None
    if has_upfront:
        start = payload.upfront_fee_start_date or date.today()
        expiry = _compute_expiry(start, payload.upfront_fee_term_years)
    else:
        start = None
        expiry = None

    row = {
        "firm_id": firm_id,
        "deal_id": deal_id,
        "recipient_name": payload.recipient_name,
        "recipient_email": payload.recipient_email,
        "arrangement_type": payload.arrangement_type,
        "implementation_fee": payload.implementation_fee,
        "upfront_fee_pct": payload.upfront_fee_pct,
        "upfront_fee_amount": payload.upfront_fee_amount,
        "upfront_fee_amount_basis": payload.upfront_fee_amount_basis,
        "include_implementation_in_wire": payload.include_implementation_in_wire,
        "upfront_fee_term_years": payload.upfront_fee_term_years,
        "upfront_fee_start_date": start.isoformat() if start else None,
        "upfront_fee_expiry_date": expiry.isoformat() if expiry else None,
        "carry_pct": payload.carry_pct,
        "carry_hurdle_pct": payload.carry_hurdle_pct,
        "notes": payload.notes,
    }
    result = supabase.table("deal_fee_arrangements").insert(row).execute()
    record = result.data[0] if result.data else row
    record["_expiry_status"] = _expiry_status(expiry, date.today())
    record["_deal"] = {"id": deal_id, "offering_name": deal_offering_name}
    return record


def _list_fee_arrangements_for_deal(deal_id: str, firm_id: str) -> list[dict]:
    today = date.today()
    rows = (
        supabase.table("deal_fee_arrangements")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .order("created_at")
        .execute()
        .data
    ) or []
    for r in rows:
        exp = date.fromisoformat(r["upfront_fee_expiry_date"]) if r.get("upfront_fee_expiry_date") else None
        r["_expiry_status"] = _expiry_status(exp, today)
    return rows


def _fee_timeline_summary_for_deals(firm_id: str, deal_ids: list[str]) -> dict[str, dict]:
    """Per deal_id: counts and soonest expiry (days) for dashboard badges."""
    if not deal_ids:
        return {}
    today = date.today()
    rows = (
        supabase.table("deal_fee_arrangements")
        .select("deal_id, upfront_fee_expiry_date")
        .eq("firm_id", firm_id)
        .in_("deal_id", deal_ids)
        .not_.is_("upfront_fee_expiry_date", "null")
        .execute()
        .data
    ) or []
    by_deal: dict[str, list[dict]] = {}
    for r in rows:
        by_deal.setdefault(r["deal_id"], []).append(r)
    out: dict[str, dict] = {}
    for did in deal_ids:
        arr = by_deal.get(did, [])
        days_list = []
        alert_n = 0
        for a in arr:
            exp = date.fromisoformat(a["upfront_fee_expiry_date"])
            d_rem = (exp - today).days
            days_list.append(d_rem)
            if d_rem <= EXPIRY_ALERT_DAYS:
                alert_n += 1
        out[did] = {
            "arrangement_count_with_expiry": len(arr),
            "expiring_or_expired_within_90d": alert_n,
            "soonest_expiry_days_remaining": min(days_list) if days_list else None,
        }
    return out


def _compute_kyc_missing_docs_batch(investor_ids: list[str], entity_type_map: dict[str, str]) -> dict[str, list[str]]:
    """
    For a list of investor IDs, return a dict mapping investor_id → list of missing checklist items.
    Uses a single batch query against kyc_reviews to avoid N+1.
    """
    from core.kyc_templates import get_checklist

    if not investor_ids:
        return {}

    reviews = (
        supabase.table("kyc_reviews")
        .select("investor_id, matched_docs")
        .in_("investor_id", investor_ids)
        .execute()
        .data
    )

    # Build a map: investor_id → set of received doc type strings (lowercased)
    received_map: dict[str, set[str]] = {inv_id: set() for inv_id in investor_ids}
    for r in reviews:
        inv_id = r["investor_id"]
        for doc in (r.get("matched_docs") or []):
            received_map[inv_id].add(doc.lower())

    missing_map: dict[str, list[str]] = {}
    for inv_id in investor_ids:
        entity_type = entity_type_map.get(inv_id, "Individual")
        checklist = get_checklist(entity_type)
        received = received_map[inv_id]
        missing = []
        for item in checklist:
            # Match if any significant portion of the checklist item appears in a received doc type
            item_key = item.lower().split("(")[0].strip()
            matched = any(item_key in rec or rec in item.lower() for rec in received)
            if not matched:
                missing.append(item)
        missing_map[inv_id] = missing

    return missing_map


def _compute_investor_stage(
    docusign_status: str,
    kyc_missing: list,
    wire_status: str,
    kyc_status: str = "Pending",
    wire_instructions_on_file: bool = False,
    stage_override: Optional[str] = None,
) -> str:
    """
    Compute the investor's current onboarding stage key.
    Maps to the firm's pipeline_stages config (stored in firm_settings).
    Manual overrides (On Hold, Paused, etc.) take priority over computed state.

    Stage order:
      awaiting_subdocs → out_for_signature → waiting_kyc → compliance_review
      → wire_instructions_needed → wire_pending → funded
    """
    if stage_override:
        return stage_override

    if wire_status == "Funded":
        return "funded"

    if docusign_status == "Pending":
        return "awaiting_subdocs"

    if docusign_status not in SIGNED_STATES:
        # Sent but not yet signed
        return "out_for_signature"

    # Signed — now check KYC
    if kyc_missing:
        return "waiting_kyc"

    if kyc_status in ("Reviewing", "Escalated"):
        return "compliance_review"

    # KYC approved — check wire
    if not wire_instructions_on_file:
        return "wire_instructions_needed"

    return "wire_pending"


def _unwrap_investor_embed(inv_raw: dict | list | None) -> dict:
    """Normalize PostgREST nested `investors` (object or single-element list)."""
    if not inv_raw:
        return {}
    if isinstance(inv_raw, list):
        return inv_raw[0] if inv_raw else {}
    return inv_raw


def _commitment_row_for_phase_bucket(c: dict) -> dict:
    """
    Shape a raw commitment (+ nested investors) like active_deal_hub_view rows
    for _enrich_rows_with_kyc / _bucket_by_phase.
    """
    inv = _unwrap_investor_embed(c.get("investors"))
    return {
        "investor_id": c.get("investor_id"),
        "entity_type": inv.get("entity_type") or "Individual",
        "docusign_status": c.get("docusign_status") or "",
        "wire_status": c.get("wire_status") or "",
        "wire_instructions": inv.get("wire_instructions"),
        "kyc_status": inv.get("kyc_status") or "Pending",
        "kyc_verified": bool(c.get("kyc_verified")),
        "verbal_confirmed": bool(c.get("verbal_confirmed")),
        "committed_amount": c.get("committed_amount"),
        "stage_override": c.get("stage_override"),
    }


def _bucket_by_phase(rows: list[dict]) -> dict:
    """Aggregate committed amounts into the four CEO pipeline phases."""
    buckets = {
        "pending_subdocs_amount": 0.0,
        "waiting_kyc_amount": 0.0,
        "waiting_wire_amount": 0.0,
        "fully_onboarded_amount": 0.0,
    }
    phase_map = {
        "Sub Docs Sent": "pending_subdocs_amount",
        "Waiting on KYC": "waiting_kyc_amount",
        "Wire Pending": "waiting_wire_amount",
        "Fully Onboarded": "fully_onboarded_amount",
    }
    for r in rows:
        key = phase_map.get(r.get("stage", ""), "pending_subdocs_amount")
        buckets[key] += float(r.get("committed_amount") or 0)
    return buckets


def _enrich_rows_with_kyc(rows: list[dict]) -> list[dict]:
    """
    Given deal hub rows, batch-fetch kyc_reviews and inject
    kyc_missing_docs + stage + computed flags into each row.
    """
    investor_ids = [r["investor_id"] for r in rows if r.get("investor_id")]
    entity_type_map = {r["investor_id"]: r.get("entity_type", "Individual") for r in rows if r.get("investor_id")}

    missing_map = _compute_kyc_missing_docs_batch(investor_ids, entity_type_map)

    # Also check for pending wire changes in a single batch query
    pending_wire_changes = set()
    if investor_ids:
        wire_changes = (
            supabase.table("investor_pending_changes")
            .select("investor_id")
            .in_("investor_id", investor_ids)
            .eq("field_name", "wire_instructions")
            .eq("status", "Pending")
            .execute()
            .data
        )
        pending_wire_changes = {r["investor_id"] for r in wire_changes}

    enriched = []
    for row in rows:
        inv_id = row.get("investor_id")
        kyc_missing = missing_map.get(inv_id, [])
        wire_on_file = bool(row.get("wire_instructions"))
        wire_change = inv_id in pending_wire_changes
        funding_flags = funding_source_hub_flags(
            subscriber_entity_name=row.get("entity_name"),
            funding_entity_name=row.get("funding_entity_name"),
            funding_entity_matches_kyc=row.get("funding_entity_matches_kyc"),
            funding_entity_kyc_status=row.get("funding_entity_kyc_status"),
            wire_status=row.get("wire_status"),
        )
        stage = _compute_investor_stage(
            docusign_status=row.get("docusign_status", ""),
            kyc_missing=kyc_missing,
            wire_status=row.get("wire_status", ""),
            kyc_status=row.get("kyc_status", "Pending"),
            wire_instructions_on_file=wire_on_file,
            stage_override=row.get("stage_override"),
        )
        enriched.append({
            **row,
            "kyc_missing_docs": kyc_missing,
            # investors.wire_instructions = distribution payout (firm → investor), not inbound subscription wire.
            "wire_instructions_purpose": "distribution_payout",
            "wire_instructions_on_file": wire_on_file,
            "distribution_payout_wire_on_file": wire_on_file,
            "wire_instructions_pending_change": wire_change,
            **funding_flags,
            "stage": stage,
            "action_needed": bool(kyc_missing) or funding_flags["funding_source_action_needed"],
            "distribution_ready": (
                not kyc_missing
                and wire_on_file
                and row.get("kyc_verified", False)
                and row.get("verbal_confirmed", False)
            ),
            "wire_change_in_flight": wire_change,
        })
    return enriched


def _fetch_commitment_extras_for_hub(cids: list) -> list[dict]:
    """Load PW / funding fields; tolerate dev DBs missing newer commitment columns."""
    full_select = (
        "id, deal_id, memorandum_number, fee_amount, "
        "liquidation_required, liquidation_due_date, liquidation_needed, cash_shortfall, "
        "liquidation_acknowledged_at, trader_id, "
        "funding_entity_name, funding_entity_matches_kyc, funding_entity_kyc_status, wire_status"
    )
    slim_select = (
        "id, deal_id, memorandum_number, fee_amount, "
        "liquidation_required, liquidation_due_date, liquidation_needed, cash_shortfall, "
        "liquidation_acknowledged_at, trader_id, wire_status"
    )
    try:
        return (
            supabase.table("commitments")
            .select(full_select)
            .in_("id", cids)
            .execute()
            .data
        ) or []
    except Exception as exc:
        msg = str(exc)
        if "funding_entity_name" not in msg and "42703" not in msg:
            raise
        import logging

        logging.getLogger(__name__).warning(
            "commitments funding columns missing — apply 20260524_funding_source_tracker.sql; using slim hub select"
        )
        return (
            supabase.table("commitments")
            .select(slim_select)
            .in_("id", cids)
            .execute()
            .data
        ) or []


def _merge_pw_hub_fields(rows: list[dict], firm_id: str) -> None:
    """
    active_deal_hub_view may omit new PW / liquidation columns — merge from base tables for ops UI.
    Mutates rows in place.
    """
    if not rows:
        return
    cids = [r["commitment_id"] for r in rows if r.get("commitment_id")]
    cmap: dict[str, dict] = {}
    if cids:
        extra = _fetch_commitment_extras_for_hub(cids)
        cmap = {e["id"]: e for e in extra}
        for r in rows:
            cid = r.get("commitment_id")
            if not cid or cid not in cmap:
                continue
            m = cmap[cid]
            for k in (
                "memorandum_number",
                "fee_amount",
                "liquidation_required",
                "liquidation_due_date",
                "liquidation_needed",
                "cash_shortfall",
                "liquidation_acknowledged_at",
                "trader_id",
                "funding_entity_name",
                "funding_entity_matches_kyc",
                "funding_entity_kyc_status",
                "wire_status",
            ):
                r[k] = m.get(k)

    inv_ids = list({r.get("investor_id") for r in rows if r.get("investor_id")})
    if inv_ids:
        invs = (
            supabase.table("investors")
            .select("id, private_wealth, schwab_estimated_liquid_cash, client_associate_email")
            .in_("id", inv_ids)
            .execute()
            .data
        ) or []
        imap = {i["id"]: i for i in invs}
        for r in rows:
            iid = r.get("investor_id")
            if not iid or iid not in imap:
                continue
            inv = imap[iid]
            r["private_wealth"] = inv.get("private_wealth")
            r["schwab_estimated_liquid_cash"] = inv.get("schwab_estimated_liquid_cash")
            r["client_associate_email"] = inv.get("client_associate_email")

    from core.pw_liquidation import get_commitment_total_wire_due

    for r in rows:
        cid = r.get("commitment_id")
        deal_id_row = cmap.get(cid, {}).get("deal_id") if cid else None
        if not deal_id_row or r.get("committed_amount") is None:
            continue
        try:
            r["total_wire_due"] = get_commitment_total_wire_due(
                r.get("committed_amount"),
                deal_id_row,
                firm_id,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "total_wire_due failed for commitment %s", cid
            )


# ---------------------------------------------------------------------------
# Active Deal Dashboard
# ---------------------------------------------------------------------------

@router.get("/active")
def list_active_deals(
    role: str = Query(default="ops", description="ops | ceo | advisor"),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
):
    """
    Return all active deals with aggregate totals.
    Response shape is scoped by role:
    - ops: unchanged — firm-wide totals per deal + fee_timeline
    - ceo: same totals + per-deal phase_buckets (committed $ by pipeline stage) + fee_timeline
    - advisor: only deals with ≥1 matching investor; your_* book totals + fund_pct_of_target
    """
    firm_id = _require_firm(x_firm_id)
    role_norm = (role or "ops").strip().lower()
    if role_norm not in ("ops", "ceo", "advisor"):
        role_norm = "ops"

    if role_norm == "advisor":
        adv_header = (x_advisor_email or "").strip()
        if not adv_header:
            raise HTTPException(
                status_code=400,
                detail="X-Advisor-Email header is required when role=advisor.",
            )
        advisor_key = adv_header.lower()
    else:
        advisor_key = ""

    _commit_ops_select = (
        "committed_amount, funded_amount, fee_amount, docusign_status, wire_status, status, "
        "investor_id, kyc_verified, verbal_confirmed"
    )
    _commit_ceo_select = _commit_ops_select + ", investors(entity_type, wire_instructions)"
    _commit_advisor_select = _commit_ops_select + ", investors(entity_type, wire_instructions, advisor_email)"

    query = (
        supabase.table("deals")
        .select("id, offering_name, target_raise, status, created_at")
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .order("created_at", desc=True)
    )
    deals = query.execute().data

    results = []
    deal_ids: list[str] = []
    for deal in deals:
        if role_norm == "ceo":
            commit_select = _commit_ceo_select
        elif role_norm == "advisor":
            commit_select = _commit_advisor_select
        else:
            commit_select = _commit_ops_select

        commitments = (
            supabase.table("commitments")
            .select(commit_select)
            .eq("deal_id", deal["id"])
            .eq("status", "Active")
            .execute()
            .data
        )
        active_all = [c for c in commitments if c["status"] == "Active"]

        if role_norm == "advisor":
            active = []
            for c in active_all:
                inv = _unwrap_investor_embed(c.get("investors"))
                em = (inv.get("advisor_email") or "").strip().lower()
                if em == advisor_key:
                    active.append(c)
            if not active:
                continue
            fund_total_committed = sum(float(c["committed_amount"] or 0) for c in active_all)
            fund_investor_count = len(active_all)
        else:
            active = active_all

        total_committed = sum(float(c["committed_amount"] or 0) for c in active)
        target = float(deal.get("target_raise") or 0)
        investor_count = len(active)
        fund_pct = round((total_committed / target * 100), 1) if target else None

        created_raw = deal["created_at"]
        if isinstance(created_raw, str):
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        else:
            created_dt = created_raw
        if getattr(created_dt, "tzinfo", None) is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        days_open = (datetime.now(timezone.utc) - created_dt).days

        if role_norm == "advisor":
            base = {
                **deal,
                "your_investor_count": investor_count,
                "your_total_committed": total_committed,
                "your_total_funded": sum(float(c["funded_amount"] or 0) for c in active),
                "your_total_fees_accrued": sum(float(c["fee_amount"] or 0) for c in active),
                "fund_pct_of_target": (
                    round((fund_total_committed / target * 100), 1) if target else None
                ),
                "near_investor_limit": fund_investor_count >= 95,
                "days_open": days_open,
            }
        else:
            base = {
                **deal,
                "investor_count": investor_count,
                "total_committed": total_committed,
                "total_funded": sum(float(c["funded_amount"] or 0) for c in active),
                "total_fees_accrued": sum(float(c["fee_amount"] or 0) for c in active),
                "pct_of_target": fund_pct,
                "near_investor_limit": investor_count >= 95,
                "days_open": days_open,
            }

        if role_norm == "ceo":
            hub_like = [_commitment_row_for_phase_bucket(c) for c in active]
            enriched = _enrich_rows_with_kyc(hub_like)
            base["phase_buckets"] = _bucket_by_phase(enriched)

        results.append(base)
        deal_ids.append(deal["id"])

    if role_norm in ("ops", "ceo") and deal_ids:
        timelines = _fee_timeline_summary_for_deals(firm_id, deal_ids)
        for base in results:
            base["fee_timeline"] = timelines.get(base["id"], {
                "arrangement_count_with_expiry": 0,
                "expiring_or_expired_within_90d": 0,
                "soonest_expiry_days_remaining": None,
            })

    if role_norm == "ceo":
        return {"deals": results, "role": "ceo"}
    if role_norm == "advisor":
        return {"deals": results, "role": "advisor"}
    return {"deals": results, "role": role_norm}


@router.get("/{deal_id}/hub")
def get_deal_hub(
    deal_id: str,
    role: str = Query(default="ops", description="ops | ceo | advisor | client_associate"),
    include_investors: bool = Query(
        default=False,
        description="When role=ceo, include full per-investor hub rows (same shape as ops) plus phase buckets in summary.",
    ),
    x_firm_id: Optional[str] = Header(default=None),
    x_advisor_email: Optional[str] = Header(default=None),
    x_ca_key: Optional[str] = Header(default=None, alias="X-CA-Key"),
):
    """
    Full deal hub drill-in. Response is scoped by role:
    - ops: all investors, all fields, all action flags
    - ceo: deal summary + money bucketed by pipeline phase; add include_investors=true for the same per-investor rows as ops (drill-down)
    - advisor: their clients only, plain-language stage, last followup date
    - client_associate: X-CA-Key (client_associates.api_key); optional X-Firm-ID must match the key's firm.
      Rows: private-wealth only where investors.client_associate_email matches the CA desk email.
    """
    if role == "client_associate":
        ca_row, firm_id = resolve_client_associate(x_ca_key, x_firm_id)
    else:
        firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("*")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    if role == "client_associate":
        ca = (ca_row.get("email") or "").strip().lower()
        raw = (
            supabase.table("commitments")
            .select(
                "id, committed_amount, funded_amount, wire_status, docusign_status, "
                "liquidation_required, liquidation_due_date, liquidation_needed, cash_shortfall, "
                "liquidation_acknowledged_at, trader_id, status, "
                "investors(entity_name, private_wealth, client_associate_email, schwab_estimated_liquid_cash, advisor_email)"
            )
            .eq("deal_id", deal_id)
            .eq("firm_id", firm_id)
            .eq("status", "Active")
            .execute()
            .data
        ) or []
        tids = list({c.get("trader_id") for c in raw if c.get("trader_id")})
        tmap: dict[str, dict] = {}
        if tids:
            trs = (
                supabase.table("traders")
                .select("id, display_name, email")
                .in_("id", tids)
                .execute()
                .data
            ) or []
            tmap = {str(t["id"]): t for t in trs}
        from core.pw_liquidation import get_commitment_total_wire_due

        investors_out = []
        for c in raw:
            inv = c.get("investors") or {}
            if not inv.get("private_wealth"):
                continue
            if (inv.get("client_associate_email") or "").strip().lower() != ca:
                continue
            tid = str(c["trader_id"]) if c.get("trader_id") else None
            tr = tmap.get(tid) if tid else None
            tw = get_commitment_total_wire_due(c.get("committed_amount"), deal_id, firm_id)
            investors_out.append({
                "commitment_id": c["id"],
                "entity_name": inv.get("entity_name"),
                "committed_amount": c.get("committed_amount"),
                "total_wire_due": tw,
                "funded_amount": c.get("funded_amount"),
                "wire_status": c.get("wire_status"),
                "docusign_status": c.get("docusign_status"),
                "schwab_estimated_liquid_cash": inv.get("schwab_estimated_liquid_cash"),
                "liquidation_required": c.get("liquidation_required"),
                "liquidation_needed": c.get("liquidation_needed"),
                "cash_shortfall": c.get("cash_shortfall"),
                "liquidation_due_date": c.get("liquidation_due_date"),
                "liquidation_acknowledged_at": c.get("liquidation_acknowledged_at"),
                "trader_desk": (tr or {}).get("display_name"),
                "trader_email": (tr or {}).get("email"),
                "advisor_email": inv.get("advisor_email"),
            })
        return {
            "deal": {k: deal[k] for k in ("id", "offering_name", "target_raise", "status") if k in deal},
            "summary": {
                "your_pw_commitment_count": len(investors_out),
                "your_total_committed": sum(float(x.get("committed_amount") or 0) for x in investors_out),
                "your_total_wire_due": sum(float(x.get("total_wire_due") or 0) for x in investors_out),
            },
            "investors": investors_out,
            "role": "client_associate",
            "filter": "private_wealth_and_your_book_only",
        }

    rows = (
        supabase.table("active_deal_hub_view")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )

    active_rows = [r for r in rows if r.get("commitment_status") == "Active"]
    total_committed = sum(float(r["committed_amount"] or 0) for r in active_rows)
    investor_count = len(active_rows)
    target = float(deal.get("target_raise") or 0)

    summary = {
        "investor_count": investor_count,
        "total_committed": total_committed,
        "total_funded": sum(float(r["funded_amount"] or 0) for r in active_rows),
        "total_fees_accrued": sum(float(r["fee_amount"] or 0) for r in active_rows),
        "pct_of_target": round((total_committed / target * 100), 1) if target else None,
        "near_investor_limit": investor_count >= 95,
    }

    fee_arrangements = _list_fee_arrangements_for_deal(deal_id, firm_id)
    fee_alerts = sum(1 for fa in fee_arrangements if fa.get("_expiry_status", {}).get("alert"))
    summary["fee_arrangement_alert_count"] = fee_alerts

    # Enrich rows with KYC data (batch query)
    enriched = _enrich_rows_with_kyc(active_rows)
    _merge_pw_hub_fields(enriched, firm_id)

    role_lower = (role or "ops").strip().lower()
    if role_lower == "ceo" and include_investors:
        phase_buckets = _bucket_by_phase(enriched)
        return {
            "deal": deal,
            "summary": {**summary, **phase_buckets},
            "fee_arrangements": fee_arrangements,
            "investors": enriched,
            "role": "ceo",
        }
    if role_lower == "ceo":
        phase_buckets = _bucket_by_phase(enriched)
        return {
            "deal": {k: deal[k] for k in ("id", "offering_name", "target_raise", "status", "fund_manager") if k in deal},
            "summary": {**summary, **phase_buckets},
            "fee_arrangements": fee_arrangements,
            "role": "ceo",
        }

    if role_lower == "advisor":
        if not x_advisor_email:
            raise HTTPException(
                status_code=400,
                detail="X-Advisor-Email header is required when role=advisor.",
            )
        advisor_email = x_advisor_email
        advisor_rows = [r for r in enriched if r.get("advisor_email") == advisor_email] if advisor_email else enriched
        return {
            "deal": {k: deal[k] for k in ("id", "offering_name", "target_raise", "status") if k in deal},
            "summary": {
                "your_investor_count": len(advisor_rows),
                "your_total_committed": sum(float(r["committed_amount"] or 0) for r in advisor_rows),
                "fund_pct_of_target": summary["pct_of_target"],
                "near_investor_limit": summary["near_investor_limit"],
            },
            "investors": [
                {
                    "entity_name": r.get("entity_name"),
                    "entity_type": r.get("entity_type"),
                    "committed_amount": r.get("committed_amount"),
                    "stage": r.get("stage"),
                    "kyc_missing_docs": r.get("kyc_missing_docs"),
                    "last_followup_at": r.get("last_followup_at"),
                    "handle_with_care": r.get("handle_with_care", False),
                }
                for r in advisor_rows
            ],
            "role": "advisor",
        }

    # Default: ops view — full detail (and any unknown role treated as ops)
    return {
        "deal": deal,
        "summary": summary,
        "fee_arrangements": fee_arrangements,
        "investors": enriched,
        "role": "ops",
    }


@router.post("/{deal_id}/hub/request-missing-info")
def request_missing_info(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Scan all investors in the deal for missing KYC docs, inbound funding-entity gaps, or (if funded)
    missing distribution payout wire instructions on file.
    Sends a targeted email to each investor with only their outstanding items listed.
    Respects the handle_with_care flag (ops CC'd on all sensitive client emails).
    """
    firm_id = _require_firm(x_firm_id)
    settings = _get_firm_settings(firm_id)

    rows = (
        supabase.table("active_deal_hub_view")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )
    active_rows = [r for r in rows if r.get("commitment_status") == "Active"]
    _merge_pw_hub_fields(active_rows, firm_id)
    enriched = _enrich_rows_with_kyc(active_rows)

    from core.graph_client import send_email

    sent = []
    skipped = []

    for row in enriched:
        investor_id = row.get("investor_id")
        kyc_missing = row.get("kyc_missing_docs", [])
        wire_on_file = row.get("distribution_payout_wire_on_file", True)
        funding_action = row.get("funding_source_action_needed", False)
        is_funded = (row.get("wire_status") or "") == "Funded"

        if not kyc_missing and not funding_action and (wire_on_file or not is_funded):
            skipped.append(row.get("entity_name"))
            continue

        investor = (
            supabase.table("investors")
            .select("primary_email, sharepoint_link, handle_with_care")
            .eq("id", investor_id)
            .single()
            .execute()
            .data
        )
        if not investor or not investor.get("primary_email"):
            skipped.append(row.get("entity_name"))
            continue

        cc = []
        if investor.get("handle_with_care") and settings.get("ops_mailbox"):
            cc = [settings["ops_mailbox"]]

        if kyc_missing:
            missing_list = "\n".join(f"  \u2022 {item}" for item in kyc_missing)
            subject = f"Outstanding KYC Documents Required \u2014 {row.get('offering_name', deal_id)}"
            body = (
                f"Dear {row.get('entity_name')},\n\n"
                f"We are still awaiting the following documents to complete your KYC review "
                f"for {row.get('offering_name', 'your investment')}:\n\n"
                f"{missing_list}\n\n"
                f"Please upload to your secure folder: {investor.get('sharepoint_link', '[link on file]')}\n\n"
                f"Thank you,\nOperations Team"
            )
            send_email(settings=settings, to=investor["primary_email"], cc=cc, subject=subject, body=body)

        if is_funded and not wire_on_file:
            from core.email_templates import build_wire_missing_request_email
            wire_email = build_wire_missing_request_email(
                entity_name=row.get("entity_name", ""),
                offering_name=row.get("offering_name", ""),
                ops_contact_email=settings.get("ops_mailbox"),
                firm_id=firm_id,
            )
            body = (
                wire_email["body"]
                + "\n\nNote: We need your bank details on file for future distributions "
                "(where we send capital back to you). This is separate from the fund's "
                "wire instructions used for your subscription payment."
            )
            send_email(
                settings=settings,
                to=investor["primary_email"],
                cc=cc,
                subject=wire_email["subject"],
                body=body,
            )

        # Record follow-up timestamp on commitment
        if row.get("commitment_id"):
            supabase.table("commitments").update({
                "last_followup_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["commitment_id"]).execute()

        sent.append(row.get("entity_name"))

    return {
        "status": "outreach_complete",
        "deal_id": deal_id,
        "emails_sent": len(sent),
        "investors_contacted": sent,
        "skipped_no_action_needed": skipped,
    }


class MassEmailPayload(BaseModel):
    subject: str
    body: str
    filter: str = "all"  # all | missing_kyc | missing_wire | funded | unfunded


@router.post("/{deal_id}/hub/mass-email")
def mass_email(
    deal_id: str,
    payload: MassEmailPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Send a mass email to all (or a filtered subset of) investors in the deal."""
    firm_id = _require_firm(x_firm_id)
    settings = _get_firm_settings(firm_id)

    rows = (
        supabase.table("active_deal_hub_view")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )
    active_rows = [r for r in rows if r.get("commitment_status") == "Active"]
    enriched = _enrich_rows_with_kyc(active_rows)

    if payload.filter == "missing_kyc":
        targets = [r for r in enriched if r.get("kyc_missing_docs")]
    elif payload.filter == "missing_wire":
        targets = [r for r in enriched if not r.get("wire_instructions_on_file")]
    elif payload.filter == "funded":
        targets = [r for r in enriched if r.get("wire_status") == "Funded"]
    elif payload.filter == "unfunded":
        targets = [r for r in enriched if r.get("wire_status") != "Funded"]
    else:
        targets = enriched

    from core.distribution_bot import send_bcc_blast
    emails = []
    for r in targets:
        inv = supabase.table("investors").select("primary_email").eq("id", r["investor_id"]).single().execute().data
        if inv and inv.get("primary_email"):
            emails.append(inv["primary_email"])

    if emails:
        send_bcc_blast(
            settings=settings,
            subject=payload.subject,
            body=payload.body,
            investor_emails=emails,
        )

    return {"status": "sent", "deal_id": deal_id, "recipients": len(emails), "filter": payload.filter}


@router.get("/{deal_id}/hub/wire-instructions")
def get_deal_wire_instructions(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Pull all outbound wire instructions for funded investors in this deal.
    Formatted for the wire operator when running a distribution.
    """
    firm_id = _require_firm(x_firm_id)

    commitments = (
        supabase.table("commitments")
        .select("id, funded_amount, investor_id, investors(entity_name, wire_instructions)")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )

    wire_records = []
    missing_wire = []
    for c in commitments:
        inv = c.get("investors", {})
        wi = inv.get("wire_instructions")
        if wi:
            wire_records.append({
                "investor_id": c["investor_id"],
                "entity_name": inv.get("entity_name"),
                "funded_amount": c.get("funded_amount"),
                "wire_instructions": wi,
            })
        else:
            missing_wire.append(inv.get("entity_name"))

    return {
        "deal_id": deal_id,
        "wire_records": wire_records,
        "investors_missing_wire": missing_wire,
        "total_with_wire": len(wire_records),
        "total_missing_wire": len(missing_wire),
    }


# ---------------------------------------------------------------------------
# Fund Ledger (Closed / Dissolved deals)
# ---------------------------------------------------------------------------

@router.get("/ledger")
def list_fund_ledger(
    x_firm_id: Optional[str] = Header(default=None),
):
    """Return all closed and dissolved deals — the Fund Ledger."""
    firm_id = _require_firm(x_firm_id)

    deals = (
        supabase.table("deals")
        .select("id, offering_name, target_raise, status, closed_at, fund_manager, created_at")
        .eq("firm_id", firm_id)
        .in_("status", ["Closed", "Dissolved"])
        .order("closed_at", desc=True)
        .execute()
        .data
    )

    results = []
    for deal in deals:
        commitments = (
            supabase.table("commitments")
            .select("funded_amount, fee_amount, status")
            .eq("deal_id", deal["id"])
            .eq("firm_id", firm_id)
            .eq("status", "Active")
            .execute()
            .data
        )
        last_dist = (
            supabase.table("distributions")
            .select("distribution_date, total_amount")
            .eq("deal_id", deal["id"])
            .order("distribution_date", desc=True)
            .limit(1)
            .execute()
            .data
        )
        results.append({
            **deal,
            "investor_count": len(commitments),
            "total_funded_aum": sum(float(c["funded_amount"] or 0) for c in commitments),
            "last_distribution_date": last_dist[0]["distribution_date"] if last_dist else None,
            "last_distribution_amount": last_dist[0]["total_amount"] if last_dist else None,
        })

    return {"funds": results}


@router.get("/{deal_id}/ledger")
def get_fund_ledger(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Fund Ledger drill-in for a closed/dissolved deal.
    Returns per-investor distribution readiness, wire status, KYC verification,
    verbal confirm status, and physical mail preferences.
    """
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("*")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")
    if deal.get("status") not in ("Closed", "Dissolved"):
        raise HTTPException(status_code=400, detail="This endpoint is for closed or dissolved deals. Use /hub for active deals.")

    commitments = (
        supabase.table("commitments")
        .select(
            "id, funded_amount, committed_amount, advisory_fee_pct, commitment_date, "
            "kyc_verified, verbal_confirmed, verbal_confirmed_at, verbal_confirmed_by, "
            "investor_id, "
            "investors(entity_name, entity_type, advisor_email, wire_instructions, "
            "orion_match_status, prefers_physical_mail, handle_with_care)"
        )
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    )

    investor_ids = [c["investor_id"] for c in commitments]
    entity_type_map = {c["investor_id"]: c.get("investors", {}).get("entity_type", "Individual") for c in commitments}
    missing_map = _compute_kyc_missing_docs_batch(investor_ids, entity_type_map)

    # Check pending wire changes in batch
    pending_wire_changes = set()
    if investor_ids:
        changes = (
            supabase.table("investor_pending_changes")
            .select("investor_id")
            .in_("investor_id", investor_ids)
            .eq("field_name", "wire_instructions")
            .eq("status", "Pending")
            .execute()
            .data
        )
        pending_wire_changes = {r["investor_id"] for r in changes}

    # Last distribution per investor
    last_dist_map: dict[str, dict] = {}
    if investor_ids:
        try:
            notices = (
                supabase.table("distribution_notices")
                .select(
                    "investor_id, individual_amount, sent_at, "
                    "distributions(distribution_date)"
                )
                .in_("investor_id", investor_ids)
                .execute()
                .data
            ) or []
        except Exception:
            notices = []
        for n in notices:
            inv_id = n["investor_id"]
            dist = n.get("distributions") or {}
            if dist and dist.get("distribution_date"):
                existing = last_dist_map.get(inv_id)
                notice_sent = n.get("sent_at")
                if not existing or dist["distribution_date"] > existing.get("date", ""):
                    last_dist_map[inv_id] = {
                        "date": dist["distribution_date"],
                        "amount": n.get("individual_amount"),
                        "sent_at": notice_sent,
                    }

    investor_rows = []
    for c in commitments:
        inv = c.get("investors", {})
        inv_id = c["investor_id"]
        wire_on_file = bool(inv.get("wire_instructions"))
        last_dist = last_dist_map.get(inv_id, {})
        kyc_missing = missing_map.get(inv_id, [])

        # Skip verbal confirm if distribution was sent within last 90 days
        skip_verbal = False
        if last_dist.get("sent_at"):
            try:
                sent_at = datetime.fromisoformat(last_dist["sent_at"])
                days_since = (datetime.now(timezone.utc) - sent_at).days
                skip_verbal = days_since < 90
            except Exception:
                pass

        investor_rows.append({
            "investor_id": inv_id,
            "commitment_id": c["id"],
            "entity_name": inv.get("entity_name"),
            "entity_type": inv.get("entity_type"),
            "advisor_email": inv.get("advisor_email"),
            "handle_with_care": inv.get("handle_with_care", False),
            "funded_amount": c.get("funded_amount"),
            "committed_amount": c.get("committed_amount"),
            "advisory_fee_pct": c.get("advisory_fee_pct"),
            "commitment_date": c.get("commitment_date"),
            "wire_instructions_on_file": wire_on_file,
            "wire_instructions_pending_change": inv_id in pending_wire_changes,
            "kyc_verified": c.get("kyc_verified", False),
            "kyc_missing_docs": kyc_missing,
            "verbal_confirmed": c.get("verbal_confirmed", False),
            "verbal_confirmed_at": c.get("verbal_confirmed_at"),
            "verbal_confirmed_by": c.get("verbal_confirmed_by"),
            "skip_verbal_confirm": skip_verbal,
            "last_distribution_date": last_dist.get("date"),
            "last_distribution_amount": last_dist.get("amount"),
            "days_since_last_distribution": (
                (datetime.now(timezone.utc) - datetime.fromisoformat(last_dist["sent_at"])).days
                if last_dist.get("sent_at") else None
            ),
            "orion_match_status": inv.get("orion_match_status"),
            "prefers_physical_mail": inv.get("prefers_physical_mail", False),
            "distribution_ready": (
                wire_on_file
                and c.get("kyc_verified", False)
                and (c.get("verbal_confirmed", False) or skip_verbal)
            ),
        })

    total_funded = sum(float(r["funded_amount"] or 0) for r in investor_rows)
    last_dist_all = (
        supabase.table("distributions")
        .select("distribution_date, total_amount")
        .eq("deal_id", deal_id)
        .order("distribution_date", desc=True)
        .limit(1)
        .execute()
        .data
    )

    return {
        "deal": deal,
        "summary": {
            "investor_count": len(investor_rows),
            "total_funded_aum": total_funded,
            "distribution_ready_count": sum(1 for r in investor_rows if r["distribution_ready"]),
            "missing_wire_count": sum(1 for r in investor_rows if not r["wire_instructions_on_file"]),
            "missing_kyc_count": sum(1 for r in investor_rows if r["kyc_missing_docs"]),
            "last_distribution_date": last_dist_all[0]["distribution_date"] if last_dist_all else None,
        },
        "investors": investor_rows,
    }


# ---------------------------------------------------------------------------
# Deal CRUD
# ---------------------------------------------------------------------------

class NewDealPayload(BaseModel):
    offering_name: str
    target_raise: Optional[float] = None
    fund_manager: Optional[str] = None
    fund_manager_title: Optional[str] = None
    fee_arrangement: Optional[FeeArrangementPayload] = None


class WireInstructionsPayload(BaseModel):
    """
    Structured wire instructions for the fund's bank account.
    Ops enters these once when the fund's account is set up.
    They are attached to Email 2 after the investor signs.
    """
    bank_name: str
    account_name: str
    account_number: str
    routing_number: Optional[str] = None  # ABA for domestic
    swift_code: Optional[str] = None       # SWIFT/BIC for international
    iban: Optional[str] = None
    bank_address: Optional[str] = None
    further_credit: Optional[str] = None   # FFC — investor entity name note
    reference: Optional[str] = None        # default memo text ops wants on all wires
    notes: Optional[str] = None            # any additional instructions


@router.post("")
def create_deal(payload: NewDealPayload, x_firm_id: Optional[str] = Header(default=None)):
    """
    Create a new deal/fund. Appears immediately in the Active Deal Dashboard.

    Optionally include `fee_arrangement` to record third-party economics (implementation fee,
    upfront fee term default 3 years, carry) in one step — same payload shape as
    POST /deals/{deal_id}/fee-arrangements.
    """
    firm_id = _require_firm(x_firm_id)

    result = supabase.table("deals").insert({
        "firm_id": firm_id,
        "offering_name": payload.offering_name,
        "target_raise": payload.target_raise,
        "fund_manager": payload.fund_manager,
        "fund_manager_title": payload.fund_manager_title,
        "status": "Active",
    }).execute()

    deal_row = result.data[0]
    fee_created = None
    if payload.fee_arrangement:
        fee_created = _insert_fee_arrangement(
            firm_id, deal_row["id"], deal_row["offering_name"], payload.fee_arrangement
        )

    return {"status": "created", "deal": deal_row, "fee_arrangement": fee_created}


@router.post("/{deal_id}/wire-instructions")
def set_deal_wire_instructions(
    deal_id: str,
    payload: WireInstructionsPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Save the fund's outbound wire instructions (bank account details).
    These are attached to Email 2 after every investor signs their sub docs.
    Ops enters this once when the fund's dedicated bank account is set up.
    """
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    wire_data = payload.model_dump(exclude_none=True)

    supabase.table("deals").update({
        "wire_instructions": wire_data,
    }).eq("id", deal_id).execute()

    return {
        "status": "wire_instructions_saved",
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "wire_instructions": wire_data,
    }


@router.get("/{deal_id}/wire-instructions")
def get_deal_outbound_wire_instructions(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Return the fund's outbound wire instructions (for ops reference / Email 2 preview).
    """
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, wire_instructions")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    return {
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "wire_instructions": deal.get("wire_instructions"),
        "wire_instructions_pdf_filename": deal.get("wire_instructions_pdf_filename"),
        "formatted": _format_wire_instructions(deal.get("wire_instructions")),
    }


@router.post("/{deal_id}/wire-instructions/upload-pdf")
async def upload_wire_instructions_pdf(
    deal_id: str,
    request: Request,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Upload the bank's official wire instructions PDF for a deal.
    The PDF is saved to the deal's SharePoint folder.
    When wire_delivery_mode = 'secure_link', investors see a folder link in
    Email 2 instead of inline wire text — they download this PDF themselves.

    Expects: Content-Type: application/pdf, raw bytes in request body.
    Optional header: X-Filename (defaults to '{OfferingName}_WireInstructions.pdf')
    """
    from core.graph_client import save_document_to_sharepoint_deal_folder

    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, sharepoint_folder_url")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    settings = _get_firm_settings(firm_id)

    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="No file content received.")

    custom_filename = request.headers.get("X-Filename")
    if custom_filename:
        filename = custom_filename
    else:
        fund_slug = deal["offering_name"].replace(" ", "_")
        filename = f"{fund_slug}_WireInstructions.pdf"

    # Save to the deal's SharePoint folder
    try:
        save_document_to_sharepoint_deal_folder(
            settings=settings,
            deal=deal,
            filename=filename,
            file_bytes=pdf_bytes,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SharePoint upload failed: {e}")

    # Record the filename so Email 2 can reference it
    supabase.table("deals").update({
        "wire_instructions_pdf_filename": filename,
    }).eq("id", deal_id).execute()

    return {
        "status": "wire_pdf_uploaded",
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "filename": filename,
        "note": "Set firm_settings.wire_delivery_mode = 'secure_link' to send investors a folder link instead of inline wire text.",
    }


class DocuSignTemplatePayload(BaseModel):
    """
    Map of entity-type categories → DocuSign template IDs for a specific deal.
    Ops sets these once when creating or configuring a new deal.
    The platform then routes each investor to the correct template automatically.
    """
    individual_template_id: Optional[str] = None
    trust_template_id: Optional[str] = None
    joint_template_id: Optional[str] = None
    entity_template_id: Optional[str] = None
    advisory_template_id: Optional[str] = None
    email_subject_template: Optional[str] = None


@router.post("/{deal_id}/docusign-template")
def set_deal_docusign_templates(
    deal_id: str,
    payload: DocuSignTemplatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Assign DocuSign template IDs to a deal.
    When a sub doc envelope is sent, the platform looks up the investor's entity type
    and selects the matching template ID automatically.
    """
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    templates: dict = {}
    if payload.individual_template_id:
        templates["Individual"] = payload.individual_template_id
    if payload.trust_template_id:
        templates["Trust"] = payload.trust_template_id
    if payload.joint_template_id:
        templates["Joint"] = payload.joint_template_id
    if payload.entity_template_id:
        templates["Entity"] = payload.entity_template_id

    update_data: dict = {}
    if templates:
        update_data["docusign_templates"] = templates
    if payload.advisory_template_id:
        update_data["docusign_advisory_template_id"] = payload.advisory_template_id
    if payload.email_subject_template:
        update_data["email_subject_template"] = payload.email_subject_template

    if not update_data:
        raise HTTPException(status_code=400, detail="No template data provided.")

    supabase.table("deals").update(update_data).eq("id", deal_id).execute()

    return {
        "status": "templates_saved",
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "docusign_templates": templates,
        "advisory_template_id": payload.advisory_template_id,
        "email_subject_template": payload.email_subject_template,
    }


@router.get("/{deal_id}/readiness")
def get_deal_readiness(deal_id: str, x_firm_id: Optional[str] = Header(default=None)):
    """
    Structured pre-close checklist: sub docs, KYC, wire, wire instructions on file,
    and pending investor data changes. Requires X-Firm-ID.
    """
    firm_id = _require_firm(x_firm_id)

    ready = check_deal_readiness(deal_id, firm_id)
    if ready is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return ready


@router.post("/{deal_id}/close")
def close_deal(deal_id: str, x_firm_id: Optional[str] = Header(default=None)):
    """
    Close a deal. Requires full deal readiness (see GET /deals/{deal_id}/readiness); otherwise 409.
    On success: marks deal Closed, moves to Fund Ledger, auto-triggers Orion NAImport export.
    """
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")
    if deal["status"] == "Closed":
        raise HTTPException(status_code=400, detail="Deal is already closed.")

    commitments = (
        supabase.table("commitments")
        .select("id, wire_status, docusign_status, status")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    )
    if not commitments:
        raise HTTPException(status_code=400, detail="No active commitments found for this deal.")

    ready = check_deal_readiness(deal_id, firm_id)
    if ready is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    if not ready["ready_to_close"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "Deal is not ready to close.", "gaps": ready["investor_gaps"]},
        )

    supabase.table("deals").update({
        "status": "Closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", deal_id).execute()

    # Auto-trigger Orion NAImport export
    orion_result = {}
    orion_blocked = False
    try:
        from scripts.orion_export import run_naimport_export
        orion_result = run_naimport_export(deal_id=deal_id, firm_id=firm_id)
    except ValueError as e:
        # Raised by validate_no_pending_matches for unresolved household matches
        orion_blocked = True
        orion_result = {"blocked": True, "reason": str(e)}
        # Notify ops
        try:
            settings = _get_firm_settings(firm_id)
            from core.graph_client import send_email
            send_email(
                settings=settings,
                to=settings.get("ops_mailbox", ""),
                cc=[],
                subject=f"ACTION REQUIRED: Orion Export Blocked — {deal['offering_name']}",
                body=(
                    f"Deal '{deal['offering_name']}' has been closed, but the Orion NAImport export "
                    f"was blocked due to unresolved household matches.\n\n"
                    f"Reason: {str(e)}\n\n"
                    f"Please resolve all Orion match conflicts in the Deal Hub, then re-run the export manually."
                ),
            )
        except Exception:
            pass
    except Exception as e:
        orion_result = {"error": str(e)}

    response = {
        "status": "closed",
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "investor_count": len(commitments),
        "orion_export": orion_result,
    }
    if orion_blocked:
        response["orion_warning"] = "Orion NAImport export was blocked — unresolved household matches. Ops has been notified."

    return response


# ---------------------------------------------------------------------------
# Distribution routes
# ---------------------------------------------------------------------------

class DistributionPayload(BaseModel):
    deal_id: str
    distribution_date: str
    total_amount: float
    tpa_confirmed_total: float
    distribution_type: str = "Income"
    notes: Optional[str] = None
    wire_date: Optional[str] = None
    generate_aip: bool = True


@router.post("/distributions")
def create_distribution(
    payload: DistributionPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Initiate a distribution for a deal.
    1. Verify the deal exists.
    2. Load all funded commitments.
    3. Calculate pro-rata distribution amounts per investor.
    4. Validate total matches TPA-confirmed figure (within $0.01).
    5. Create distribution record + individual distribution_notices.
    6. Auto-generate Orion AIP import files.
    """
    from decimal import Decimal
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", payload.deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    commitments = (
        supabase.table("commitments")
        .select("id, investor_id, funded_amount, kyc_verified")
        .eq("deal_id", payload.deal_id)
        .eq("firm_id", firm_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )
    if not commitments:
        raise HTTPException(status_code=400, detail="No funded commitments found for this deal.")

    total_funded = sum(Decimal(str(c["funded_amount"] or 0)) for c in commitments)
    if total_funded <= 0:
        raise HTTPException(status_code=400, detail="Total funded amount is zero.")

    total_amount = Decimal(str(payload.total_amount))
    tpa_total = Decimal(str(payload.tpa_confirmed_total))

    notices = []
    calculated_total = Decimal("0")
    for c in commitments:
        funded = Decimal(str(c["funded_amount"] or 0))
        pro_rata = (funded / total_funded * total_amount).quantize(Decimal("0.01"))
        calculated_total += pro_rata
        notices.append({
            "investor_id": c["investor_id"],
            "commitment_id": c["id"],
            "individual_amount": float(pro_rata),
            "kyc_verified": bool(c.get("kyc_verified", False)),
        })

    delta = abs(calculated_total - tpa_total)
    if delta > Decimal("0.01"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"TPA validation FAILED: calculated ${float(calculated_total):,.2f} vs. "
                f"TPA-confirmed ${float(tpa_total):,.2f} (delta: ${float(delta):,.2f})."
            ),
        )

    distribution = supabase.table("distributions").insert({
        "firm_id": firm_id,
        "deal_id": payload.deal_id,
        "distribution_date": payload.distribution_date,
        "wire_date": payload.wire_date or payload.distribution_date,
        "total_amount": payload.total_amount,
        "distribution_type": payload.distribution_type,
        "notes": payload.notes,
        "status": "Processing",
    }).execute().data[0]

    distribution_id = distribution["id"]

    supabase.table("distribution_notices").insert([
        _distribution_notice_insert_row(
            firm_id=firm_id,
            distribution_id=distribution_id,
            investor_id=n["investor_id"],
            commitment_id=n.get("commitment_id"),
            individual_amount=n["individual_amount"],
            kyc_verified=bool(n.get("kyc_verified", False)),
        )
        for n in notices
    ]).execute()

    result = {
        "status": "distribution_created",
        "distribution_id": distribution_id,
        "deal_id": payload.deal_id,
        "investor_count": len(notices),
        "calculated_total": float(calculated_total),
        "tpa_confirmed_total": payload.tpa_confirmed_total,
        "aip_files": None,
    }

    if payload.generate_aip:
        try:
            from scripts.orion_aip_distribution_export import generate_distribution_aip
            aip = generate_distribution_aip(
                distribution_id=distribution_id,
                firm_id=firm_id,
                tpa_confirmed_total=payload.tpa_confirmed_total,
                is_dissolution=False,
            )
            result["aip_files"] = {"aip_transaction": aip["aip_transaction"], "aip_asset": aip["aip_asset"]}
        except Exception as e:
            result["aip_warning"] = f"AIP export failed (non-blocking): {e}"

    return result


@router.get("/{deal_id}/distributions")
def list_deal_distributions(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """List all distribution events for a closed fund."""
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    distributions = (
        supabase.table("distributions")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .order("distribution_date", desc=True)
        .execute()
        .data
    ) or []

    out = []
    for d in distributions:
        notice_rows = (
            supabase.table("distribution_notices")
            .select("id")
            .eq("distribution_id", d["id"])
            .eq("firm_id", firm_id)
            .execute()
            .data
        ) or []
        out.append({
            **d,
            "investor_count": len(notice_rows),
        })

    return {"deal_id": deal_id, "offering_name": deal.get("offering_name"), "distributions": out}


@router.get("/{deal_id}/distributions/status-dashboard")
def distribution_status_dashboard(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    distribution_id: Optional[str] = Query(default=None),
):
    """
    Per-investor distribution readiness dashboard for a closed deal.
    Shows KYC verified, verbal confirmed, wire on file, negative consent status.
    Pass distribution_id to target a specific event (defaults to latest).
    """
    from core.distribution_readiness import enrich_notice_row, verbal_confirm_days
    from core.distribution_event_workflow import compute_event_workflow

    firm_id = _require_firm(x_firm_id)
    settings = _get_firm_settings(firm_id)
    confirm_days = verbal_confirm_days(settings)

    dist_select = "*"

    if distribution_id:
        distribution = (
            supabase.table("distributions")
            .select(dist_select)
            .eq("id", distribution_id)
            .eq("deal_id", deal_id)
            .eq("firm_id", firm_id)
            .single()
            .execute()
            .data
        )
    else:
        latest_dist = (
            supabase.table("distributions")
            .select(dist_select)
            .eq("deal_id", deal_id)
            .eq("firm_id", firm_id)
            .order("distribution_date", desc=True)
            .limit(1)
            .execute()
            .data
        )
        distribution = latest_dist[0] if latest_dist else None

    notices_data = []
    if distribution:
        notices_data = (
            supabase.table("distribution_notices")
            .select(
                "id, investor_id, individual_amount, status, "
                "kyc_verified, verbal_confirmed, verbal_confirmed_at, negative_consent_sent_at, "
                "investors(entity_name, entity_type, primary_email, phone, wire_instructions, handle_with_care)"
            )
            .eq("distribution_id", distribution["id"])
            .eq("firm_id", firm_id)
            .execute()
            .data
        ) or []

    rows = []
    for n in notices_data:
        inv = n.get("investors") or {}
        rows.append(enrich_notice_row(n, inv, firm_id=firm_id, deal_id=deal_id, confirm_days=confirm_days))

    workflow = compute_event_workflow(distribution or {}, rows, firm_settings=settings) if distribution else None

    return {
        "deal_id": deal_id,
        "distribution": distribution,
        "verbal_confirm_days": confirm_days,
        "workflow": workflow,
        "summary": {
            "total_investors": len(rows),
            "distribution_ready": sum(1 for r in rows if r["distribution_ready"]),
            "missing_verbal": sum(
                1 for r in rows if "missing_verbal" in r.get("blockers", [])
            ),
            "missing_kyc_verified": sum(
                1 for r in rows if "missing_kyc" in r.get("blockers", [])
            ),
            "missing_wire": sum(
                1 for r in rows if "missing_wire" in r.get("blockers", [])
            ),
            "negative_consent_sent": sum(1 for r in rows if r.get("negative_consent_sent")),
        },
        "investors": rows,
    }


class VerbalConfirmPayload(BaseModel):
    confirmed_by: Optional[str] = None


@router.patch("/distribution-notices/{notice_id}/verbal-confirm")
def mark_distribution_verbal_confirmed(
    notice_id: str,
    payload: VerbalConfirmPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Ops marks verbal confirmation complete for this distribution notice."""
    from datetime import datetime, timezone

    firm_id = _require_firm(x_firm_id)

    notice = (
        supabase.table("distribution_notices")
        .select("id, firm_id, investor_id, distribution_id")
        .eq("id", notice_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not notice:
        raise HTTPException(status_code=404, detail="Distribution notice not found.")

    now = datetime.now(timezone.utc).isoformat()
    confirmed_by = (payload.confirmed_by or "ops").strip() or "ops"

    updated = (
        supabase.table("distribution_notices")
        .update({
            "verbal_confirmed": True,
            "verbal_confirmed_at": now,
            "verbal_confirmed_by": confirmed_by,
        })
        .eq("id", notice_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )

    return {"status": "confirmed", "notice": updated[0] if updated else None}


@router.post("/{deal_id}/distributions/negative-consent")
def send_negative_consent(
    deal_id: str,
    distribution_amount: float,
    distribution_date: str,
    opt_out_deadline: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Send negative consent blast to all funded investors before initiating a distribution.
    Records negative_consent_sent_at on each distribution_notice.
    Distribution cannot be finalized until consent period clears.
    """
    firm_id = _require_firm(x_firm_id)
    settings = _get_firm_settings(firm_id)

    commitments = (
        supabase.table("commitments")
        .select("investor_id, funded_amount, investors(entity_name, primary_email, wire_instructions)")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )

    from core.email_templates import build_negative_consent_email
    from core.graph_client import send_email

    total_funded = sum(float(c.get("funded_amount") or 0) for c in commitments)
    sent = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for c in commitments:
        inv = c.get("investors", {})
        if not inv.get("primary_email"):
            continue
        funded = float(c.get("funded_amount") or 0)
        pro_rata = round((funded / total_funded) * distribution_amount, 2) if total_funded else 0

        email = build_negative_consent_email(
            entity_name=inv["entity_name"],
            offering_name=deal_id,
            distribution_amount=pro_rata,
            distribution_date=distribution_date,
            opt_out_deadline=opt_out_deadline,
            ops_contact_email=settings.get("ops_mailbox"),
            firm_id=firm_id,
        )
        send_email(settings=settings, to=inv["primary_email"], cc=[], subject=email["subject"], body=email["body"])
        sent.append(inv["entity_name"])

    # Record on distribution_notices if a distribution record exists
    latest_dist = (
        supabase.table("distributions")
        .select("id")
        .eq("deal_id", deal_id)
        .eq("status", "Processing")
        .limit(1)
        .execute()
        .data
    )
    if latest_dist:
        supabase.table("distribution_notices").update({
            "negative_consent_sent_at": now_iso,
        }).eq("distribution_id", latest_dist[0]["id"]).execute()

    return {
        "status": "negative_consent_sent",
        "deal_id": deal_id,
        "investors_notified": len(sent),
        "distribution_date": distribution_date,
        "opt_out_deadline": opt_out_deadline,
    }


# ---------------------------------------------------------------------------
# Deal dissolution route
# ---------------------------------------------------------------------------

class DissolvePayload(BaseModel):
    tpa_confirmed_total: float
    distribution_date: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{deal_id}/dissolve")
def dissolve_deal(
    deal_id: str,
    payload: DissolvePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Mark a deal as dissolving and generate final return-of-capital distribution + Orion AIP files.
    Also kicks off dissolution wire/KYC collection for investors missing data.
    """
    from decimal import Decimal
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")
    if deal["status"] == "Dissolved":
        raise HTTPException(status_code=400, detail="Deal is already dissolved.")

    dist_date = payload.distribution_date or date.today().isoformat()

    commitments = (
        supabase.table("commitments")
        .select("id, investor_id, funded_amount, investors(wire_instructions, kyc_status, entity_name, primary_email)")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )
    if not commitments:
        raise HTTPException(status_code=400, detail="No funded commitments to dissolve.")

    total_funded = sum(Decimal(str(c["funded_amount"] or 0)) for c in commitments)
    tpa_total = Decimal(str(payload.tpa_confirmed_total))
    notices_data = []
    calculated_total = Decimal("0")

    for c in commitments:
        funded = Decimal(str(c["funded_amount"] or 0))
        pro_rata = (funded / total_funded * tpa_total).quantize(Decimal("0.01"))
        calculated_total += pro_rata
        notices_data.append({"investor_id": c["investor_id"], "commitment_id": c["id"], "individual_amount": float(pro_rata)})

    delta = abs(calculated_total - tpa_total)
    if delta > Decimal("0.01"):
        raise HTTPException(
            status_code=422,
            detail=f"TPA validation FAILED: calculated ${float(calculated_total):,.2f} vs. ${float(tpa_total):,.2f}.",
        )

    distribution = supabase.table("distributions").insert({
        "firm_id": firm_id,
        "deal_id": deal_id,
        "distribution_date": dist_date,
        "total_amount": payload.tpa_confirmed_total,
        "distribution_type": "Return of Capital",
        "notes": payload.notes or f"{deal['offering_name']} — final dissolution distribution",
        "status": "Processing",
    }).execute().data[0]

    distribution_id = distribution["id"]

    supabase.table("distribution_notices").insert([
        _distribution_notice_insert_row(
            firm_id=firm_id,
            distribution_id=distribution_id,
            investor_id=n["investor_id"],
            commitment_id=n.get("commitment_id"),
            individual_amount=n["individual_amount"],
        )
        for n in notices_data
    ]).execute()

    supabase.table("deals").update({
        "status": "Dissolved",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", deal_id).execute()

    # Seed dissolution_tracker for investors missing wire or KYC
    settings = _get_firm_settings(firm_id)
    tracker_rows = []
    collection_needed = []
    for c in commitments:
        inv = c.get("investors", {})
        wire_missing = not bool(inv.get("wire_instructions"))
        kyc_incomplete = inv.get("kyc_status") not in ("Approved", "Complete")
        tracker_rows.append({
            "firm_id": firm_id,
            "deal_id": deal_id,
            "investor_id": c["investor_id"],
            "wire_received": not wire_missing,
            "kyc_received": not kyc_incomplete,
        })
        if wire_missing or kyc_incomplete:
            collection_needed.append(c)

    if tracker_rows:
        supabase.table("dissolution_tracker").upsert(tracker_rows, on_conflict="deal_id,investor_id").execute()

    # Fire collection emails for investors missing data
    if collection_needed:
        from core.graph_client import send_email
        for c in collection_needed:
            inv = c.get("investors", {})
            if not inv.get("primary_email"):
                continue
            items_needed = []
            if not inv.get("wire_instructions"):
                items_needed.append("wire instructions (bank name, routing number, account number)")
            if inv.get("kyc_status") not in ("Approved", "Complete"):
                items_needed.append("updated KYC documentation")
            items_text = " and ".join(items_needed)
            send_email(
                settings=settings,
                to=inv["primary_email"],
                cc=[settings.get("ops_mailbox")] if settings.get("ops_mailbox") else [],
                subject=f"Action Required: {deal['offering_name']} — Fund Dissolution",
                body=(
                    f"Dear {inv['entity_name']},\n\n"
                    f"We are initiating the dissolution of {deal['offering_name']} and need to collect "
                    f"your {items_text} before we can process your final distribution.\n\n"
                    f"Please contact our operations team at your earliest convenience.\n\n"
                    f"Thank you,\nOperations Team"
                ),
            )

    result = {
        "status": "deal_dissolved",
        "deal_id": deal_id,
        "offering_name": deal["offering_name"],
        "distribution_id": distribution_id,
        "investor_count": len(notices_data),
        "calculated_total": float(calculated_total),
        "investors_needing_collection": len(collection_needed),
        "aip_files": None,
    }

    try:
        from scripts.orion_aip_distribution_export import generate_distribution_aip
        aip = generate_distribution_aip(
            distribution_id=distribution_id,
            firm_id=firm_id,
            tpa_confirmed_total=payload.tpa_confirmed_total,
            is_dissolution=True,
        )
        result["aip_files"] = {"aip_transaction": aip["aip_transaction"], "aip_asset": aip["aip_asset"]}
    except Exception as e:
        result["aip_warning"] = f"AIP export failed (non-blocking): {e}"

    return result


# ---------------------------------------------------------------------------
# Third-party fee arrangements
# ---------------------------------------------------------------------------

@router.post("/{deal_id}/fee-arrangements", status_code=201)
def add_fee_arrangement(
    deal_id: str,
    payload: FeeArrangementPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Add a placement agent / sub-advisor fee structure to a deal."""
    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, target_raise")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    record = _insert_fee_arrangement(firm_id, deal_id, deal["offering_name"], payload)
    refresh_pw_liquidation_for_deal(deal_id, firm_id)
    return record


@router.get("/{deal_id}/fee-arrangements")
def list_fee_arrangements(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """List all fee arrangements for a deal with expiry status annotations."""
    firm_id = _require_firm(x_firm_id)
    rows = _list_fee_arrangements_for_deal(deal_id, firm_id)
    return {"deal_id": deal_id, "count": len(rows), "arrangements": rows}


@router.patch("/{deal_id}/fee-arrangements/{arrangement_id}")
def update_fee_arrangement(
    deal_id: str,
    arrangement_id: str,
    payload: FeeArrangementUpdatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Update a fee arrangement. Re-computes expiry date if start or term changes."""
    firm_id = _require_firm(x_firm_id)

    existing = (
        supabase.table("deal_fee_arrangements")
        .select("*")
        .eq("id", arrangement_id)
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Fee arrangement not found.")

    if payload.arrangement_type and payload.arrangement_type not in ARRANGEMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"arrangement_type must be one of: {ARRANGEMENT_TYPES}")

    updates = payload.model_dump(exclude_none=True)

    merged_impl = updates.get("implementation_fee", existing.get("implementation_fee"))
    merged_inc = updates.get("include_implementation_in_wire", existing.get("include_implementation_in_wire"))
    if merged_inc and merged_impl is None:
        raise HTTPException(
            status_code=400,
            detail="include_implementation_in_wire requires implementation_fee on record or in this update.",
        )

    if updates.get("upfront_fee_amount_basis") and updates["upfront_fee_amount_basis"] not in UPFRONT_AMOUNT_BASIS:
        raise HTTPException(
            status_code=400,
            detail=f"upfront_fee_amount_basis must be one of: {sorted(UPFRONT_AMOUNT_BASIS)}",
        )

    merged = {**existing, **updates}
    has_up = merged.get("upfront_fee_pct") is not None or merged.get("upfront_fee_amount") is not None

    # Re-compute expiry if start date or term changes and expiry not manually overridden
    if "upfront_fee_expiry_date" not in updates:
        if has_up:
            start_raw = merged.get("upfront_fee_start_date")
            term = merged.get("upfront_fee_term_years") or 3
            if start_raw:
                start = date.fromisoformat(str(start_raw))
                expiry = _compute_expiry(start, int(term))
                updates["upfront_fee_expiry_date"] = expiry.isoformat() if expiry else None
            else:
                updates["upfront_fee_expiry_date"] = None
        else:
            updates["upfront_fee_expiry_date"] = None

    if "upfront_fee_start_date" in updates and isinstance(updates["upfront_fee_start_date"], date):
        updates["upfront_fee_start_date"] = updates["upfront_fee_start_date"].isoformat()
    if "upfront_fee_expiry_date" in updates and isinstance(updates["upfront_fee_expiry_date"], date):
        updates["upfront_fee_expiry_date"] = updates["upfront_fee_expiry_date"].isoformat()

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = supabase.table("deal_fee_arrangements").update(updates).eq("id", arrangement_id).execute()
    record = result.data[0] if result.data else {**existing, **updates}
    exp = date.fromisoformat(record["upfront_fee_expiry_date"]) if record.get("upfront_fee_expiry_date") else None
    record["_expiry_status"] = _expiry_status(exp, date.today())
    refresh_pw_liquidation_for_deal(deal_id, firm_id)
    return record


@router.delete("/{deal_id}/fee-arrangements/{arrangement_id}", status_code=204)
def delete_fee_arrangement(
    deal_id: str,
    arrangement_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Remove a fee arrangement from a deal."""
    firm_id = _require_firm(x_firm_id)
    supabase.table("deal_fee_arrangements").delete().eq("id", arrangement_id).eq("deal_id", deal_id).eq("firm_id", firm_id).execute()
    refresh_pw_liquidation_for_deal(deal_id, firm_id)
    return None


@router.get("/fee-arrangements/expiring")
def get_expiring_fee_arrangements(
    days: int = Query(default=EXPIRY_ALERT_DAYS, description="Warn within this many days of expiry"),
    include_expired: bool = Query(default=True, description="Include already-expired arrangements"),
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Firm-wide list of fee arrangements whose upfront fee period is expiring or has expired.
    Used for timeline alerts in the deal hub and exec dashboard.
    """
    firm_id = _require_firm(x_firm_id)
    today = date.today()
    horizon = today + timedelta(days=days)

    rows = (
        supabase.table("deal_fee_arrangements")
        .select("*, deals(id, offering_name, status)")
        .eq("firm_id", firm_id)
        .not_.is_("upfront_fee_expiry_date", "null")
        .lte("upfront_fee_expiry_date", horizon.isoformat())
        .order("upfront_fee_expiry_date")
        .execute()
        .data
    ) or []

    alerts = []
    for r in rows:
        exp = date.fromisoformat(r["upfront_fee_expiry_date"])
        days_remaining = (exp - today).days
        if days_remaining < 0 and not include_expired:
            continue
        status = _expiry_status(exp, today)
        alerts.append({
            "arrangement_id": r["id"],
            "deal_id": r["deal_id"],
            "offering_name": (r.get("deals") or {}).get("offering_name"),
            "deal_status": (r.get("deals") or {}).get("status"),
            "recipient_name": r.get("recipient_name"),
            "recipient_email": r.get("recipient_email"),
            "arrangement_type": r["arrangement_type"],
            "upfront_fee_pct": r.get("upfront_fee_pct"),
            "upfront_fee_amount": r.get("upfront_fee_amount"),
            "carry_pct": r.get("carry_pct"),
            "expiry_date": status["expiry_date"],
            "days_remaining": status["days_remaining"],
            "alert": status["alert"],
        })

    return {
        "firm_id": firm_id,
        "as_of": today.isoformat(),
        "alert_window_days": days,
        "count": len(alerts),
        "arrangements": alerts,
    }
