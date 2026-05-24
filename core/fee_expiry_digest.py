"""
Expiring third-party fee arrangements (placement / upfront term windows).

Used by:
  - Ops To-Do API (surface items in the dashboard)
  - Daily scheduler email digest to ops_mailbox
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from core.database import supabase

DEFAULT_WARN_DAYS = 90
logger = logging.getLogger(__name__)

# Appended to daily fee-expiry digest + surfaced on GET /ops/todos (firm-configurable).
FEE_EXPIRY_FOLLOWUP_STYLES = frozenset({
    "third_party_reminder",
    "management_fee_transition",
    "capital_call_planning",
    "management_and_capital",
    "custom_only",
})

_MGMT_TRANSITION_NOTE = (
    "Management fee / billing: When placement or upfront periods end, LPA and side letters often change "
    "how ongoing management fees accrue or are collected. Confirm the post-expiry schedule with finance "
    "and fund counsel and update your accounting cadence (e.g. Orion) as required."
)

_CAPITAL_CALL_NOTE = (
    "Capital activity: If your process ties placement term roll-off to draws, true-ups, or capital calls, "
    "confirm required next steps under each fund's documents. Pivot does not initiate capital calls automatically."
)


def fee_expiry_playbook_appendix(settings: dict) -> str:
    """
    Extra guidance appended to the ops email digest after the arrangement list.
    Controlled by fee_expiry_followup_style and optional fee_expiry_custom_instructions.
    """
    style = (settings.get("fee_expiry_followup_style") or "third_party_reminder").strip()
    if style not in FEE_EXPIRY_FOLLOWUP_STYLES:
        style = "third_party_reminder"

    custom = (settings.get("fee_expiry_custom_instructions") or "").strip()
    blocks: list[str] = []

    if style == "management_fee_transition":
        blocks.append(_MGMT_TRANSITION_NOTE)
    elif style == "capital_call_planning":
        blocks.append(_CAPITAL_CALL_NOTE)
    elif style == "management_and_capital":
        blocks.append(_MGMT_TRANSITION_NOTE)
        blocks.append(_CAPITAL_CALL_NOTE)
    elif style == "custom_only":
        pass  # only custom below
    # third_party_reminder: no preset blocks

    if style != "custom_only" and custom:
        blocks.append(f"Your firm's notes:\n{custom}")
    elif style == "custom_only" and custom:
        blocks.append(custom)

    return "\n\n".join(blocks) if blocks else ""


def fee_expiry_playbook_summary(settings: dict) -> dict[str, Any]:
    """For API responses (e.g. ops dashboard) so the UI can show the firm's chosen playbook."""
    style = (settings.get("fee_expiry_followup_style") or "third_party_reminder").strip()
    if style not in FEE_EXPIRY_FOLLOWUP_STYLES:
        style = "third_party_reminder"
    return {
        "fee_expiry_followup_style": style,
        "fee_expiry_custom_instructions": settings.get("fee_expiry_custom_instructions"),
        "playbook_appendix_preview": fee_expiry_playbook_appendix(settings),
        "available_styles": sorted(FEE_EXPIRY_FOLLOWUP_STYLES),
    }


def list_expiring_fee_arrangements(firm_id: str, warn_days: int = DEFAULT_WARN_DAYS) -> list[dict[str, Any]]:
    """
    Arrangements whose upfront_fee_expiry_date is within warn_days or already past.
    Sorted by expiry date (soonest first among those returned).
    """
    today = date.today()
    rows = (
        supabase.table("deal_fee_arrangements")
        .select("*, deals(id, offering_name, status)")
        .eq("firm_id", firm_id)
        .not_.is_("upfront_fee_expiry_date", "null")
        .order("upfront_fee_expiry_date")
        .execute()
        .data
    ) or []

    alerts: list[dict[str, Any]] = []
    for r in rows:
        exp = date.fromisoformat(r["upfront_fee_expiry_date"])
        days_remaining = (exp - today).days
        if days_remaining > warn_days:
            continue

        deal = r.get("deals") or {}
        alerts.append(
            {
                "arrangement_id": r["id"],
                "deal_id": r["deal_id"],
                "offering_name": deal.get("offering_name"),
                "deal_status": deal.get("status"),
                "recipient_name": r.get("recipient_name"),
                "recipient_email": r.get("recipient_email"),
                "arrangement_type": r["arrangement_type"],
                "implementation_fee": r.get("implementation_fee"),
                "upfront_fee_pct": r.get("upfront_fee_pct"),
                "upfront_fee_amount": r.get("upfront_fee_amount"),
                "upfront_fee_term_years": r.get("upfront_fee_term_years"),
                "upfront_fee_start_date": r.get("upfront_fee_start_date"),
                "upfront_fee_expiry_date": r["upfront_fee_expiry_date"],
                "carry_pct": r.get("carry_pct"),
                "carry_hurdle_pct": r.get("carry_hurdle_pct"),
                "days_remaining": days_remaining,
                "alert": "expired" if days_remaining < 0 else "expiring_soon",
                "notes": r.get("notes"),
            }
        )

    return alerts


def send_fee_expiry_ops_digest(firm_id: str, settings: dict) -> dict[str, Any]:
    """
    If enabled and ops_mailbox is set, email a single digest for all expiring/near-expiry arrangements.
    Called from the daily scheduler; use POST /ops/todos/fee-expiry-digest for manual send.
    """
    from core.graph_client import send_email

    if not settings.get("notify_ops_fee_expiry", True):
        return {"sent": False, "reason": "notify_disabled", "count": 0}

    ops_mailbox = settings.get("ops_mailbox")
    if not ops_mailbox:
        return {"sent": False, "reason": "no_ops_mailbox", "count": 0}

    warn_days = int(settings.get("fee_expiry_alert_days") or DEFAULT_WARN_DAYS)
    alerts = list_expiring_fee_arrangements(firm_id, warn_days)
    if not alerts:
        return {"sent": False, "reason": "none_due", "count": 0}

    lines = []
    for a in alerts:
        who = a.get("recipient_name") or a.get("arrangement_type") or "Counterparty"
        fund = a.get("offering_name") or "Fund"
        exp = a.get("upfront_fee_expiry_date")
        dr = a["days_remaining"]
        status = "EXPIRED" if dr < 0 else f"{dr} days left"
        lines.append(f"  • {fund} — {who} — expiry {exp} ({status})")

    body = (
        f"Third-party upfront fee terms need attention (within {warn_days} days or already expired):\n\n"
        + "\n".join(lines)
        + "\n\nOpen the Deal Hub or call GET /deals/fee-arrangements/expiring for full detail.\n"
    )
    appendix = fee_expiry_playbook_appendix(settings)
    if appendix:
        body = body + "\n---\nFOLLOW-UP (your firm's settings)\n\n" + appendix + "\n"

    send_email(
        settings=settings,
        to=ops_mailbox,
        cc=[],
        subject=f"[Altvio] Expiring placement/upfront fees ({len(alerts)})",
        body=body,
    )
    logger.info("Fee expiry ops digest sent to %s for %s arrangement(s).", ops_mailbox, len(alerts))
    return {"sent": True, "count": len(alerts), "warn_days": warn_days}
