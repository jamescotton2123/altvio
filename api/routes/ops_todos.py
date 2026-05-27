"""
Ops To-Do Dashboard — actionable task queue for operations team.

Surfaces items that require human action and can't be automated:
  - Wire instruction change requests (must verbally verify before applying)
  - Pending KYC approvals (investors waiting for ops sign-off)
  - Sub docs awaiting signature past X days (follow-up needed)
  - Expiring third-party fee arrangements (placement upfront term — window from firm_settings.fee_expiry_alert_days)

Priority ordering within each bucket:
  1. Committed amount descending — bigger tickets surface first
  2. handle_with_care flag — sensitive clients rise above same-tier tickets
  3. Days pending ascending — older items escalate within ties

Wire verification flow:
  1. Investor submits wire change via portal → investor_pending_changes row inserted
  2. GET /ops/todos returns item with investor phone, committed amount, proposed wire
  3. Ops calls investor, verbally confirms → POST /ops/todos/wire-verify/{id}
  4. Platform applies the change to investors.wire_instructions
  5. OR: POST /ops/todos/wire-reject/{id} to reject and notify investor

Fee expiry digest:
  - Daily 08:00 email to ops_mailbox when notify_ops_fee_expiry is on (see PATCH /firm/settings/ops-alerts)
  - Daily 08:00 optional email to each private-wealth trader desk + each Client Associate
    (investors.client_associate_email) when notify_trader_liquidation_digest is on
    (see PATCH /firm/settings/trader-digest); POST /ops/todos/trader-liquidation-digest to send now.
    liquidation_watch on GET /ops/todos is private-wealth clients only (investors.private_wealth).
  - fee_expiry_followup_style + fee_expiry_custom_instructions — firm playbook in digest and under expiring_fee_arrangements.playbook on GET /ops/todos
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.database import supabase
from core.fee_expiry_digest import (
    fee_expiry_playbook_summary,
    list_expiring_fee_arrangements,
    send_fee_expiry_ops_digest,
)
from core.trader_liquidation_digest import (
    list_firm_liquidation_watch,
    send_trader_liquidation_digest,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


def _get_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    return result.data or {}


# ---------------------------------------------------------------------------
# GET /ops/todos — full to-do list for ops
# ---------------------------------------------------------------------------

@router.get("")
def get_ops_todos(x_firm_id: Optional[str] = Header(default=None)):
    """
    Returns all pending action items for the ops team, grouped by type.
    Designed to be rendered as a prioritized to-do list in the ops dashboard.
    """
    firm_id = _require_firm(x_firm_id)
    settings = _get_settings(firm_id)
    warn_days = int(settings.get("fee_expiry_alert_days") or 90)
    trader_warn = int(settings.get("trader_liquidation_alert_days") or 14)

    def _safe(label: str, fn):
        try:
            return fn()
        except Exception as exc:
            logger.warning("ops todos %s failed for firm %s: %s", label, firm_id, exc)
            return []

    wire_verifications = _safe("wire_verifications", lambda: _get_wire_verifications(firm_id))
    kyc_pending = _safe("kyc_pending_review", lambda: _get_kyc_pending(firm_id))
    stale_subdocs = _safe("stale_subdocs", lambda: _get_stale_subdocs(firm_id))
    expiring_fees = _safe(
        "expiring_fee_arrangements",
        lambda: list_expiring_fee_arrangements(firm_id, warn_days),
    )
    liquidation_watch = _safe(
        "liquidation_watch",
        lambda: list_firm_liquidation_watch(firm_id, trader_warn),
    )

    total = (
        len(wire_verifications)
        + len(kyc_pending)
        + len(stale_subdocs)
        + len(expiring_fees)
        + len(liquidation_watch)
    )

    return {
        "firm_id": firm_id,
        "total_action_items": total,
        "wire_verifications": {
            "count": len(wire_verifications),
            "items": wire_verifications,
            "description": "Investors who submitted wire change requests through the portal. Call to verify before applying.",
        },
        "kyc_pending_review": {
            "count": len(kyc_pending),
            "items": kyc_pending,
            "description": "Investors whose KYC documents have been reviewed by AI and flagged for ops approval.",
        },
        "stale_subdocs": {
            "count": len(stale_subdocs),
            "items": stale_subdocs,
            "description": "Subscription documents sent 7+ days ago with no signature yet. Follow up recommended.",
        },
        "expiring_fee_arrangements": {
            "count": len(expiring_fees),
            "items": expiring_fees,
            "description": f"Third-party fee arrangements whose upfront fee term is expiring within {warn_days} days or has expired.",
            "alert_window_days": warn_days,
            "playbook": fee_expiry_playbook_summary(settings),
        },
        "liquidation_watch": {
            "count": len(liquidation_watch),
            "items": liquidation_watch,
            "description": (
                f"Private-wealth clients only: commitments flagged for liquidation funding, not yet funded, "
                f"with due date within {trader_warn} days (or past / unset). Requires investor.private_wealth."
            ),
            "alert_window_days": trader_warn,
        },
    }


@router.post("/fee-expiry-digest")
def trigger_fee_expiry_digest(x_firm_id: Optional[str] = Header(default=None)):
    """
    Send the ops email digest for expiring/expired upfront fee arrangements now.
    Normally runs automatically with the daily 08:00 job when notify_ops_fee_expiry is on.
    """
    firm_id = _require_firm(x_firm_id)
    settings = _get_settings(firm_id)
    if not settings:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return send_fee_expiry_ops_digest(firm_id, settings)


@router.post("/trader-liquidation-digest")
def trigger_trader_liquidation_digest(x_firm_id: Optional[str] = Header(default=None)):
    """
    Email PW trader desks and Client Associates (client_associate_email) their liquidation queue now.
    Same as the daily 08:00 job for this firm. Requires notify_trader_liquidation_digest.
    """
    firm_id = _require_firm(x_firm_id)
    settings = _get_settings(firm_id)
    if not settings:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return send_trader_liquidation_digest(firm_id, settings)


def _priority_sort_key(item: dict) -> tuple:
    """
    Sort key for all to-do buckets:
      1. Committed amount descending — bigger tickets first
      2. handle_with_care descending — sensitive clients surface above same-tier tickets
      3. Days pending/outstanding descending — older unresolved items escalate within ties
    All negated so Python's default ascending sort produces the correct descending order.
    """
    committed = float(item.get("committed_amount") or 0)
    sensitive = 1 if item.get("handle_with_care") else 0
    days = item.get("days_pending") or item.get("days_outstanding") or 0
    return (-committed, -sensitive, -days)


def _urgency(committed: float, handle_with_care: bool, days: int, high_days_threshold: int) -> str:
    if handle_with_care or committed >= 1_000_000 or days >= high_days_threshold:
        return "high"
    if committed >= 500_000 or days >= high_days_threshold // 2:
        return "elevated"
    return "normal"


def _get_wire_verifications(firm_id: str) -> list[dict]:
    rows = (
        supabase.table("investor_pending_changes")
        .select(
            "id, investor_id, proposed_value, created_at, "
            "investors(entity_name, primary_email, phone, entity_type, "
            "handle_with_care, sensitivity_notes, commitments(committed_amount))"
        )
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .execute()
        .data
    ) or []

    results = []
    for row in rows:
        investor = row.get("investors") or {}
        proposed = row.get("proposed_value")
        if isinstance(proposed, str):
            try:
                proposed = json.loads(proposed)
            except Exception:
                proposed = {"raw": proposed}

        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            days_pending = (datetime.now(timezone.utc) - created).days
        except Exception:
            days_pending = 0

        # Max committed amount across all commitments for this investor
        commitments = investor.get("commitments") or []
        committed_amount = max((float(c.get("committed_amount") or 0) for c in commitments), default=0.0)
        handle_with_care = bool(investor.get("handle_with_care"))

        results.append({
            "change_id": row["id"],
            "investor_id": row["investor_id"],
            "entity_name": investor.get("entity_name", "Unknown"),
            "entity_type": investor.get("entity_type", ""),
            "email": investor.get("primary_email", ""),
            "phone": investor.get("phone", "Not on file"),
            "handle_with_care": handle_with_care,
            "sensitivity_notes": investor.get("sensitivity_notes", ""),
            "committed_amount": committed_amount,
            "days_pending": days_pending,
            "submitted_at": row["created_at"],
            "proposed_wire": proposed,
            "action_label": "Call to verbally verify wire instructions",
            "urgency": _urgency(committed_amount, handle_with_care, days_pending, high_days_threshold=2),
        })

    results.sort(key=_priority_sort_key)
    return results


def _infer_kyc_confidence(review: dict) -> str:
    if review.get("escalated_to_compliance"):
        return "low"
    flags = review.get("flags") or []
    if len(flags) >= 3:
        return "low"
    if len(flags) >= 1:
        return "medium"
    return "high"


def _get_kyc_pending(firm_id: str) -> list[dict]:
    rows = (
        supabase.table("kyc_reviews")
        .select(
            "id, investor_id, flags, escalated_to_compliance, "
            "investors(entity_name, primary_email, phone, entity_type, kyc_status, "
            "handle_with_care, sensitivity_notes, commitments(committed_amount))"
        )
        .eq("firm_id", firm_id)
        .eq("status", "Pending")
        .execute()
        .data
    ) or []

    results = []
    for row in rows:
        investor = row.get("investors") or {}
        days_pending = 0

        commitments = investor.get("commitments") or []
        committed_amount = max((float(c.get("committed_amount") or 0) for c in commitments), default=0.0)
        handle_with_care = bool(investor.get("handle_with_care"))

        results.append({
            "review_id": row["id"],
            "investor_id": row["investor_id"],
            "entity_name": investor.get("entity_name", "Unknown"),
            "entity_type": investor.get("entity_type", ""),
            "email": investor.get("primary_email", ""),
            "phone": investor.get("phone", "Not on file"),
            "handle_with_care": handle_with_care,
            "sensitivity_notes": investor.get("sensitivity_notes", ""),
            "committed_amount": committed_amount,
            "confidence": _infer_kyc_confidence(row),
            "flags": row.get("flags") or [],
            "days_pending": days_pending,
            "action_label": "Review AI-extracted KYC data and approve or request more documents",
            "urgency": _urgency(committed_amount, handle_with_care, days_pending, high_days_threshold=3),
        })

    results.sort(key=_priority_sort_key)
    return results


def _get_stale_subdocs(firm_id: str, stale_after_days: int = 7) -> list[dict]:
    rows = (
        supabase.table("commitments")
        .select(
            "id, investor_id, deal_id, committed_amount, created_at, "
            "investors(entity_name, primary_email, phone, advisor_email, "
            "handle_with_care, sensitivity_notes), deals(offering_name)"
        )
        .eq("firm_id", firm_id)
        .eq("docusign_status", "Sent")
        .execute()
        .data
    ) or []

    results = []
    for row in rows:
        try:
            sent_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            days_outstanding = (datetime.now(timezone.utc) - sent_at).days
        except Exception:
            days_outstanding = 0

        if days_outstanding < stale_after_days:
            continue

        investor = row.get("investors") or {}
        deal = row.get("deals") or {}
        committed_amount = float(row.get("committed_amount") or 0)
        handle_with_care = bool(investor.get("handle_with_care"))

        results.append({
            "commitment_id": row["id"],
            "investor_id": row["investor_id"],
            "entity_name": investor.get("entity_name", "Unknown"),
            "email": investor.get("primary_email", ""),
            "phone": investor.get("phone", "Not on file"),
            "advisor_email": investor.get("advisor_email", ""),
            "handle_with_care": handle_with_care,
            "sensitivity_notes": investor.get("sensitivity_notes", ""),
            "committed_amount": committed_amount,
            "offering_name": deal.get("offering_name", ""),
            "days_outstanding": days_outstanding,
            "sent_at": row["created_at"],
            "action_label": f"Sub docs sent {days_outstanding} days ago — follow up with investor or advisor",
            "urgency": _urgency(committed_amount, handle_with_care, days_outstanding, high_days_threshold=14),
        })

    results.sort(key=_priority_sort_key)
    return results


# ---------------------------------------------------------------------------
# Wire verification actions
# ---------------------------------------------------------------------------

class WireVerifyPayload(BaseModel):
    confirmed_by: str  # ops user name / email who made the call
    notes: Optional[str] = None


class WireRejectPayload(BaseModel):
    reason: str
    rejected_by: str


@router.post("/wire-verify/{change_id}")
def verify_wire_change(
    change_id: str,
    payload: WireVerifyPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Ops has verbally confirmed the wire change. Apply it to the investor record.
    Marks the pending change as Confirmed and updates investors.wire_instructions.
    """
    firm_id = _require_firm(x_firm_id)

    change = (
        supabase.table("investor_pending_changes")
        .select("*, investors(entity_name, primary_email)")
        .eq("id", change_id)
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .single()
        .execute()
        .data
    )
    if not change:
        raise HTTPException(status_code=404, detail="Pending wire change not found.")

    proposed = change.get("proposed_value")
    if isinstance(proposed, str):
        try:
            proposed = json.loads(proposed)
        except Exception:
            raise HTTPException(status_code=400, detail="Could not parse proposed wire instructions.")

    # Apply to investor record
    supabase.table("investors").update({
        "wire_instructions": proposed,
    }).eq("id", change["investor_id"]).execute()

    # Mark change as confirmed
    supabase.table("investor_pending_changes").update({
        "status": "Approved",
        "confirmed_by": payload.confirmed_by,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", change_id).execute()

    # Notify investor that wire instructions have been updated
    investor = change.get("investors") or {}
    if investor.get("primary_email"):
        settings = _get_settings(firm_id)
        try:
            from core.graph_client import send_email
            firm_name = settings.get("firm_name", "Operations Team")
            send_email(
                settings=settings,
                to=investor["primary_email"],
                cc=[],
                subject=f"Wire Instructions Updated — {firm_name}",
                body=f"""Dear {investor.get('entity_name', 'Investor')},

Your wire instructions have been updated in our system following verbal confirmation with our operations team.

If you did not authorize this change or have any concerns, please contact us immediately at {settings.get('ops_mailbox', '')}.

{firm_name}
""",
            )
        except Exception as e:
            logger.warning("Wire confirm email failed: %s", e)

    logger.info("Wire change %s verified and applied by %s.", change_id, payload.confirmed_by)
    return {
        "status": "applied",
        "change_id": change_id,
        "investor_id": change["investor_id"],
        "confirmed_by": payload.confirmed_by,
        "message": f"Wire instructions updated for {investor.get('entity_name', 'investor')}.",
    }


@router.post("/wire-reject/{change_id}")
def reject_wire_change(
    change_id: str,
    payload: WireRejectPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Ops rejects the wire change (could not verify, investor not reached, suspected fraud).
    Notifies the investor and clears the pending item.
    """
    firm_id = _require_firm(x_firm_id)

    change = (
        supabase.table("investor_pending_changes")
        .select("*, investors(entity_name, primary_email)")
        .eq("id", change_id)
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .single()
        .execute()
        .data
    )
    if not change:
        raise HTTPException(status_code=404, detail="Pending wire change not found.")

    supabase.table("investor_pending_changes").update({
        "status": "Rejected",
        "confirmed_by": payload.rejected_by,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", change_id).execute()

    investor = change.get("investors") or {}
    if investor.get("primary_email"):
        settings = _get_settings(firm_id)
        try:
            from core.graph_client import send_email
            send_email(
                settings=settings,
                to=investor["primary_email"],
                cc=[],
                subject="Wire Instruction Update — Not Applied",
                body=f"""Dear {investor.get('entity_name', 'Investor')},

We were unable to complete the verification of your recent wire instruction update request.

Reason: {payload.reason}

Your wire instructions on file have not been changed. If you would like to update your wire instructions, please contact our operations team directly at {settings.get('ops_mailbox', '')}.

Operations Team
""",
            )
        except Exception as e:
            logger.warning("Wire reject email failed: %s", e)

    return {
        "status": "rejected",
        "change_id": change_id,
        "rejected_by": payload.rejected_by,
        "reason": payload.reason,
    }

