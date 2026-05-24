"""
Executive Command Center — top-line dashboard for CEO / Managing Partners.

Fully customizable: each firm configures which widgets appear, in what order,
and what thresholds drive urgency labels via firm_settings.exec_dashboard_config.

Widget IDs: aip_summary | capital_velocity | fund_progress | pipeline_health |
            advisor_desk_health | investor_leaderboard | ops_pulse | recent_activity

PATCH /exec/config  — update widget config
GET   /exec/config  — read current config with available widget definitions
GET   /exec/dashboard — live data, shaped by current config
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.commitment_status import SIGNED_STATES
from core.database import supabase

router = APIRouter()

# ---------------------------------------------------------------------------
# Default config — used when firm has no customization yet
# ---------------------------------------------------------------------------

DEFAULT_EXEC_CONFIG = {
    "widgets": [
        {"id": "aip_summary",          "enabled": True,  "position": 1, "label": "AIP Summary"},
        {"id": "capital_velocity",     "enabled": True,  "position": 2, "label": "Capital Velocity"},
        {"id": "fund_progress",        "enabled": True,  "position": 3, "label": "Fund Progress"},
        {"id": "pipeline_health",      "enabled": True,  "position": 4, "label": "Pipeline Health"},
        {"id": "advisor_desk_health",  "enabled": True,  "position": 5, "label": "Advisor Desk Health"},
        {"id": "investor_leaderboard", "enabled": True,  "position": 6, "label": "Top Investors"},
        {"id": "ops_pulse",            "enabled": True,  "position": 7, "label": "Ops Pulse"},
        {"id": "recent_activity",      "enabled": True,  "position": 8, "label": "Recent Activity"},
    ],
    "thresholds": {
        "stale_subdoc_days": 7,
        "velocity_period": "month",   # "month" or "quarter"
        "leaderboard_count": 10,
        "activity_count": 20,
    },
    "show_advisory_fees": True,
    "show_fund_targets": True,
}

WIDGET_DESCRIPTIONS = {
    "aip_summary":          "Total committed, funded, unfunded AIP and advisory fees earned.",
    "capital_velocity":     "New commitments and funded capital over the selected period.",
    "fund_progress":        "Per-fund breakdown: target, committed, funded, days to close.",
    "pipeline_health":      "Investor count at each stage: KYC → Sub Docs → Signed → Funded.",
    "advisor_desk_health":  "Weekly advisor book health: capital by desk, pipeline friction, executive brief for partners.",
    "investor_leaderboard": "Top investors by total committed across all funds.",
    "ops_pulse":            "Open action items: wire verifications, KYC reviews, stale sub docs, expiring fees, PW liquidation watch (Schwab book only).",
    "recent_activity":      "Live feed of onboardings, signings, fundings, and status changes.",
}


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


def _widget_advisor_desk_health(firm_id: str) -> dict:
    from core.advisor_insights import build_executive_insights_view

    return build_executive_insights_view(firm_id)


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None


def _get_exec_config(firm_id: str) -> dict:
    """Load firm's exec dashboard config, merging with defaults for any missing keys."""
    settings = (
        supabase.table("firm_settings")
        .select("exec_dashboard_config")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    ) or {}
    saved = settings.get("exec_dashboard_config") or {}
    config = {**DEFAULT_EXEC_CONFIG, **saved}
    # Merge thresholds separately so partial overrides work
    config["thresholds"] = {**DEFAULT_EXEC_CONFIG["thresholds"], **(saved.get("thresholds") or {})}
    return config


def _enabled_widgets(config: dict) -> set[str]:
    return {w["id"] for w in config.get("widgets", []) if w.get("enabled", True)}


def _widget_order(config: dict) -> dict[str, int]:
    return {w["id"]: w.get("position", 99) for w in config.get("widgets", [])}


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

class ExecConfigPayload(BaseModel):
    widgets: Optional[list[dict]] = None
    thresholds: Optional[dict] = None
    show_advisory_fees: Optional[bool] = None
    show_fund_targets: Optional[bool] = None


@router.get("/config")
def get_exec_config(x_firm_id: Optional[str] = Header(default=None)):
    """Return current exec dashboard config with widget descriptions."""
    firm_id = _require_firm(x_firm_id)
    config = _get_exec_config(firm_id)
    # Annotate each widget with its description
    for w in config.get("widgets", []):
        w["description"] = WIDGET_DESCRIPTIONS.get(w["id"], "")
    return {"firm_id": firm_id, "config": config}


@router.patch("/config")
def update_exec_config(
    payload: ExecConfigPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Update exec dashboard config. Partial updates supported — only provided
    fields are changed. Widget list replaces the full widgets array if provided.
    """
    firm_id = _require_firm(x_firm_id)
    current = _get_exec_config(firm_id)

    if payload.widgets is not None:
        # Validate widget IDs
        valid_ids = set(WIDGET_DESCRIPTIONS.keys())
        for w in payload.widgets:
            if w.get("id") not in valid_ids:
                raise HTTPException(status_code=400, detail=f"Unknown widget id: {w.get('id')}. Valid: {sorted(valid_ids)}")
        current["widgets"] = payload.widgets
    if payload.thresholds is not None:
        current["thresholds"] = {**current["thresholds"], **payload.thresholds}
    if payload.show_advisory_fees is not None:
        current["show_advisory_fees"] = payload.show_advisory_fees
    if payload.show_fund_targets is not None:
        current["show_fund_targets"] = payload.show_fund_targets

    supabase.table("firm_settings").update({
        "exec_dashboard_config": current,
    }).eq("firm_id", firm_id).execute()

    return {"status": "updated", "config": current}


# ---------------------------------------------------------------------------
# Main dashboard endpoint
# ---------------------------------------------------------------------------

@router.get("")
def get_exec_dashboard(x_firm_id: Optional[str] = Header(default=None)):
    """
    Returns the full Executive Command Center payload.
    All monetary values in USD. All counts are live from the database.
    """
    firm_id = _require_firm(x_firm_id)
    config = _get_exec_config(firm_id)
    enabled = _enabled_widgets(config)
    thresholds = config.get("thresholds", DEFAULT_EXEC_CONFIG["thresholds"])
    show_advisory_fees = config.get("show_advisory_fees", True)
    show_fund_targets = config.get("show_fund_targets", True)
    stale_days = int(thresholds.get("stale_subdoc_days", 7))
    leaderboard_count = int(thresholds.get("leaderboard_count", 10))
    activity_count = int(thresholds.get("activity_count", 20))
    velocity_period = thresholds.get("velocity_period", "month")

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    quarter_month = ((now.month - 1) // 3) * 3 + 1
    quarter_start = now.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    primary_start = month_start if velocity_period == "month" else quarter_start

    # Only run expensive queries for enabled widgets
    needs_commitments = bool(enabled & {"aip_summary", "capital_velocity", "fund_progress",
                                        "pipeline_health", "investor_leaderboard", "ops_pulse"})

    commitments = (
        supabase.table("commitments")
        .select(
            "id, committed_amount, funded_amount, advisory_fee_pct, "
            "docusign_status, wire_status, created_at, commitment_date, "
            "investors(id, entity_name, kyc_status, wire_instructions, advisor_id), "
            "deals(id, offering_name, target_raise, status, close_date, fund_manager)"
        )
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) if needs_commitments else []

    deals = (
        supabase.table("deals")
        .select("id, offering_name, target_raise, status, close_date, fund_manager")
        .eq("firm_id", firm_id)
        .execute()
        .data
    ) if "fund_progress" in enabled else []

    recent_events = (
        supabase.table("commitment_events")
        .select("commitment_id, event_type, changed_at, new_value, investors(entity_name), deals(offering_name)")
        .eq("firm_id", firm_id)
        .order("changed_at", desc=True)
        .limit(activity_count)
        .execute()
        .data
    ) if "recent_activity" in enabled else []

    # -----------------------------------------------------------------------
    # AIP Summary
    # -----------------------------------------------------------------------
    total_committed = sum(float(c.get("committed_amount") or 0) for c in commitments)
    total_funded = sum(float(c.get("funded_amount") or 0) for c in commitments)
    total_unfunded = total_committed - total_funded

    total_advisory_fees = sum(
        float(c.get("funded_amount") or 0) * float(c.get("advisory_fee_pct") or 1.0) / 100
        for c in commitments
    ) if show_advisory_fees else None

    # -----------------------------------------------------------------------
    # Capital Velocity
    # -----------------------------------------------------------------------
    def _in_period(c: dict, start: datetime) -> bool:
        dt = _parse_dt(c.get("created_at"))
        return dt is not None and dt >= start

    def _funded_in_period(c: dict, start: datetime) -> bool:
        dt = _parse_dt(c.get("commitment_date"))
        return dt is not None and dt >= start and float(c.get("funded_amount") or 0) > 0

    new_committed_primary = sum(
        float(c.get("committed_amount") or 0)
        for c in commitments if _in_period(c, primary_start)
    )
    new_committed_secondary = sum(
        float(c.get("committed_amount") or 0)
        for c in commitments if _in_period(c, quarter_start if velocity_period == "month" else month_start)
    )
    funded_primary = sum(
        float(c.get("funded_amount") or 0)
        for c in commitments if _funded_in_period(c, primary_start)
    )
    new_investors_primary = len({
        c["investors"]["id"] for c in commitments
        if _in_period(c, primary_start) and c.get("investors")
    })

    # -----------------------------------------------------------------------
    # Fund Progress
    # -----------------------------------------------------------------------
    fund_map: dict[str, dict] = {}
    for deal in deals:
        fund_map[deal["id"]] = {
            "deal_id": deal["id"],
            "offering_name": deal.get("offering_name", ""),
            "fund_manager": deal.get("fund_manager", ""),
            "target_raise": float(deal.get("target_raise") or 0),
            "status": deal.get("status", ""),
            "close_date": deal.get("close_date"),
            "total_committed": 0.0,
            "total_funded": 0.0,
            "investor_count": 0,
            "signed_count": 0,
            "funded_count": 0,
            "kyc_pending_count": 0,
        }

    for c in commitments:
        deal = c.get("deals") or {}
        deal_id = deal.get("id")
        if not deal_id or deal_id not in fund_map:
            continue
        fm = fund_map[deal_id]
        fm["total_committed"] += float(c.get("committed_amount") or 0)
        fm["total_funded"] += float(c.get("funded_amount") or 0)
        fm["investor_count"] += 1
        if c.get("docusign_status") in SIGNED_STATES:
            fm["signed_count"] += 1
        if float(c.get("funded_amount") or 0) > 0:
            fm["funded_count"] += 1
        investor = c.get("investors") or {}
        if investor.get("kyc_status") not in ("Approved", None):
            fm["kyc_pending_count"] += 1

    for fm in fund_map.values():
        target = fm["target_raise"]
        fm["pct_committed"] = round(fm["total_committed"] / target * 100, 1) if target else None
        fm["pct_funded"] = round(fm["total_funded"] / target * 100, 1) if target else None
        if not show_fund_targets:
            fm["target_raise"] = None
            fm["pct_committed"] = None
            fm["pct_funded"] = None
        days_to_close = None
        if fm.get("close_date"):
            try:
                close_dt = datetime.fromisoformat(fm["close_date"])
                close_dt = close_dt.replace(tzinfo=timezone.utc) if close_dt.tzinfo is None else close_dt
                days_to_close = (close_dt - now).days
            except Exception:
                pass
        fm["days_to_close"] = days_to_close

    active_funds = sorted(
        [f for f in fund_map.values() if f["status"] == "Active"],
        key=lambda f: f["total_committed"],
        reverse=True,
    )
    closed_funds = [f for f in fund_map.values() if f["status"] != "Active"]

    # -----------------------------------------------------------------------
    # Investor Leaderboard — top 10 by total committed
    # -----------------------------------------------------------------------
    investor_totals: dict[str, dict] = {}
    for c in commitments:
        investor = c.get("investors") or {}
        inv_id = investor.get("id")
        if not inv_id:
            continue
        if inv_id not in investor_totals:
            investor_totals[inv_id] = {
                "entity_name": investor.get("entity_name", ""),
                "total_committed": 0.0,
                "total_funded": 0.0,
                "fund_count": 0,
            }
        investor_totals[inv_id]["total_committed"] += float(c.get("committed_amount") or 0)
        investor_totals[inv_id]["total_funded"] += float(c.get("funded_amount") or 0)
        investor_totals[inv_id]["fund_count"] += 1

    leaderboard = sorted(
        investor_totals.values(),
        key=lambda x: x["total_committed"],
        reverse=True,
    )[:leaderboard_count]

    # -----------------------------------------------------------------------
    # Pipeline Health — investors by stage
    # -----------------------------------------------------------------------
    pipeline: dict[str, int] = {
        "kyc_pending": 0,
        "kyc_approved_awaiting_subdocs": 0,
        "subdocs_sent": 0,
        "subdocs_signed": 0,
        "funded": 0,
        "missing_wire": 0,
    }
    for c in commitments:
        investor = c.get("investors") or {}
        kyc = investor.get("kyc_status", "")
        ds = c.get("docusign_status", "")
        funded = float(c.get("funded_amount") or 0) > 0
        has_wire = bool(investor.get("wire_instructions"))

        if funded:
            pipeline["funded"] += 1
            if not has_wire:
                pipeline["missing_wire"] += 1
        elif ds in SIGNED_STATES:
            pipeline["subdocs_signed"] += 1
            if not has_wire:
                pipeline["missing_wire"] += 1
        elif ds == "Sent":
            pipeline["subdocs_sent"] += 1
        elif kyc == "Approved":
            pipeline["kyc_approved_awaiting_subdocs"] += 1
        else:
            pipeline["kyc_pending"] += 1

    # -----------------------------------------------------------------------
    # Ops Pulse — surface counts so CEO knows if ops is on top of things
    # -----------------------------------------------------------------------
    pending_wire_changes = (
        supabase.table("investor_pending_changes")
        .select("id", count="exact")
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .execute()
        .count
    ) or 0

    stale_subdoc_count = sum(
        1 for c in commitments
        if c.get("docusign_status") == "Sent"
        and _parse_dt(c.get("created_at")) is not None
        and (now - _parse_dt(c["created_at"])).days >= stale_days
    )

    kyc_review_pending = (
        supabase.table("kyc_reviews")
        .select("id", count="exact")
        .eq("firm_id", firm_id)
        .eq("status", "Pending")
        .execute()
        .count
    ) or 0

    # Expiring fee arrangements + private-wealth liquidation watch (firm-configured windows)
    expiring_fees_count = 0
    liquidation_watch_count = 0
    if "ops_pulse" in enabled:
        from core.fee_expiry_digest import list_expiring_fee_arrangements
        from core.trader_liquidation_digest import list_firm_liquidation_watch

        fs = (
            supabase.table("firm_settings")
            .select("fee_expiry_alert_days, trader_liquidation_alert_days")
            .eq("firm_id", firm_id)
            .single()
            .execute()
            .data
        ) or {}
        warn_days = int(fs.get("fee_expiry_alert_days") or 90)
        trader_warn = int(fs.get("trader_liquidation_alert_days") or 14)
        expiring_fees_count = len(list_expiring_fee_arrangements(firm_id, warn_days))
        liquidation_watch_count = len(list_firm_liquidation_watch(firm_id, trader_warn))

    ops_pulse = {
        "wire_verifications_pending": pending_wire_changes,
        "stale_subdocs": stale_subdoc_count,
        "kyc_reviews_pending": kyc_review_pending,
        "expiring_fee_arrangements": expiring_fees_count,
        "liquidation_watch": liquidation_watch_count,
        "total_open_items": (
            pending_wire_changes
            + stale_subdoc_count
            + kyc_review_pending
            + expiring_fees_count
            + liquidation_watch_count
        ),
    }

    # -----------------------------------------------------------------------
    # Recent Activity Feed
    # -----------------------------------------------------------------------
    activity = []
    event_labels = {
        "docusign_status_changed": "DocuSign status updated",
        "wire_status_changed": "Wire status updated",
        "kyc_status_changed": "KYC status updated",
        "funded": "Wire received — commitment funded",
        "commitment_created": "New commitment created",
        "side_letter_attached": "Side letter attached",
        "portal_link_regenerated": "Portal link regenerated",
        "loi_sent": "LOI sent",
    }
    for event in recent_events:
        investor = event.get("investors") or {}
        deal = event.get("deals") or {}
        activity.append({
            "event_type": event.get("event_type"),
            "label": event_labels.get(event.get("event_type", ""), event.get("event_type", "")),
            "entity_name": investor.get("entity_name", ""),
            "offering_name": deal.get("offering_name", ""),
            "value": event.get("new_value"),
            "occurred_at": event.get("changed_at"),
        })

    period_label = "MTD" if velocity_period == "month" else "QTD"

    all_widgets = {
        "aip_summary": {
            "total_committed": total_committed,
            "total_funded": total_funded,
            "total_unfunded": total_unfunded,
            "funding_rate_pct": round(total_funded / total_committed * 100, 1) if total_committed else 0,
            "total_advisory_fees_earned": round(total_advisory_fees, 2) if total_advisory_fees is not None else None,
            "total_investors": len(investor_totals),
            "total_active_funds": len(active_funds),
        },
        "capital_velocity": {
            "period": velocity_period,
            f"new_committed_{period_label.lower()}": new_committed_primary,
            f"new_committed_{'qtd' if velocity_period == 'month' else 'mtd'}": new_committed_secondary,
            f"funded_{period_label.lower()}": funded_primary,
            f"new_investors_{period_label.lower()}": new_investors_primary,
        },
        "fund_progress": {
            "active_funds": active_funds,
            "closed_funds": closed_funds,
        },
        "pipeline_health": pipeline,
        "advisor_desk_health": _widget_advisor_desk_health(firm_id),
        "investor_leaderboard": leaderboard,
        "ops_pulse": ops_pulse,
        "recent_activity": activity,
    }

    # Only return enabled widgets, sorted by configured position
    response_widgets = {
        wid: data
        for wid, data in all_widgets.items()
        if wid in enabled
    }

    return {
        "generated_at": now.isoformat(),
        "firm_id": firm_id,
        "widget_order": sorted(
            [w for w in config.get("widgets", []) if w.get("enabled")],
            key=lambda w: w.get("position", 99),
        ),
        **response_widgets,
    }


# ---------------------------------------------------------------------------
# Advisor desk health (Executive Command Center — not ops to-do list)
# ---------------------------------------------------------------------------


@router.get("/advisor-insights")
def get_executive_advisor_insights(x_firm_id: Optional[str] = Header(default=None)):
    """
    Latest weekly advisor desk report for leadership: firm roll-up, rankings, executive brief.
    """
    firm_id = _require_firm(x_firm_id)
    from core.advisor_insights import build_executive_insights_view

    return build_executive_insights_view(firm_id)


@router.get("/advisor-insights/{advisor_email}")
def get_executive_advisor_insight_drilldown(
    advisor_email: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Drill into one advisor desk from the latest weekly report."""
    firm_id = _require_firm(x_firm_id)
    from core.advisor_insights import get_latest_advisor_insight

    row = get_latest_advisor_insight(firm_id, advisor_email)
    if not row:
        raise HTTPException(status_code=404, detail="No desk report found for this advisor.")
    return row


@router.post("/advisor-insights/generate")
def generate_executive_advisor_insights(x_firm_id: Optional[str] = Header(default=None)):
    """On-demand weekly advisor desk report for the Executive Command Center."""
    firm_id = _require_firm(x_firm_id)
    from core.advisor_insights import generate_advisor_insights

    try:
        result = generate_advisor_insights(firm_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Insight generation failed: {e}") from e

    return {"status": "generated", "firm_id": firm_id, **result}
