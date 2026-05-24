"""
Metered billing materialization — aligns with BUSINESS_MODEL.md.

Pricing (code is source of truth; update if BUSINESS_MODEL changes):
  - onboarding_complete: $75 per commitment once investor is fully through pipeline
    (KYC Approved + DocuSigned) for that commitment.
  - aip_bps_quarterly: 1.5 basis points per year on total funded capital, invoiced
    according to the selected billing granularity (monthly or quarterly).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.commitment_status import KYC_APPROVED_STATES, SIGNED_STATES
from core.database import supabase

# $75 per onboarded investor commitment
ONBOARDING_FEE_CENTS = 7500
# 1.5 bps = 0.015% = 0.00015 of funded principal per year
AIP_BPS_ANNUAL = Decimal("0.00015")


def _sum_funded_for_firm(firm_id: str) -> Decimal:
    rows = (
        supabase.table("commitments")
        .select("funded_amount")
        .eq("firm_id", firm_id)
        .execute()
        .data
        or []
    )
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r.get("funded_amount") or 0))
    return total


def materialize_billing_period(
    firm_id: str,
    billing_period: str,
    granularity: str = "quarterly",
) -> dict[str, Any]:
    """
    Idempotently create billing_usage lines and refresh the draft billing_invoices row.

    billing_period: calendar bucket, e.g. '2026-05' or '2026-Q2' — must match how you
    invoice; keep consistent for UNIQUE constraints.
    granularity: 'monthly' divides annual AIP bps by 12; 'quarterly' divides by 4.
    """
    if granularity not in {"monthly", "quarterly"}:
        raise ValueError("granularity must be 'monthly' or 'quarterly'")

    # --- Existing onboarding usage (one row per commitment lifetime) ---
    existing_onb = (
        supabase.table("billing_usage")
        .select("commitment_id")
        .eq("firm_id", firm_id)
        .eq("event_type", "onboarding_complete")
        .execute()
        .data
        or []
    )
    onboarded_commitment_ids = {r["commitment_id"] for r in existing_onb if r.get("commitment_id")}

    # --- Qualifying commitments: signed sub docs + KYC approved on investor ---
    commitments = (
        supabase.table("commitments")
        .select("id, investor_id, investors(kyc_status), docusign_status")
        .eq("firm_id", firm_id)
        .execute()
        .data
        or []
    )

    onboarding_created = 0
    for row in commitments:
        inv = row.get("investors")
        if isinstance(inv, list):
            inv = inv[0] if inv else {}
        if row.get("docusign_status") not in SIGNED_STATES:
            continue
        if (inv or {}).get("kyc_status") not in KYC_APPROVED_STATES:
            continue
        cid = row["id"]
        if cid in onboarded_commitment_ids:
            continue
        supabase.table("billing_usage").insert({
            "firm_id": firm_id,
            "event_type": "onboarding_complete",
            "amount_cents": ONBOARDING_FEE_CENTS,
            "commitment_id": cid,
            "billing_period": billing_period,
            "metadata": {"pricing_version": "2026-05"},
        }).execute()
        onboarded_commitment_ids.add(cid)
        onboarding_created += 1

    # --- AIP bps accrual line for this period ---
    total_funded = _sum_funded_for_firm(firm_id)
    annual_fee = total_funded * AIP_BPS_ANNUAL
    divisor = Decimal(12 if granularity == "monthly" else 4)
    period_fee = annual_fee / divisor
    aip_cents = int((period_fee * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    aip_rows = (
        supabase.table("billing_usage")
        .select("id")
        .eq("firm_id", firm_id)
        .eq("event_type", "aip_bps_quarterly")
        .eq("billing_period", billing_period)
        .execute()
        .data
        or []
    )
    aip_payload = {
        "amount_cents": aip_cents,
        "metadata": {
            "total_funded": str(total_funded),
            "pricing_version": "2026-05",
            "granularity": granularity,
        },
    }
    if aip_rows:
        supabase.table("billing_usage").update(aip_payload).eq("id", aip_rows[0]["id"]).execute()
    else:
        supabase.table("billing_usage").insert({
            "firm_id": firm_id,
            "event_type": "aip_bps_quarterly",
            "billing_period": billing_period,
            **aip_payload,
        }).execute()

    # --- Draft invoice roll-up for this period ---
    usage_rows = (
        supabase.table("billing_usage")
        .select("*")
        .eq("firm_id", firm_id)
        .eq("billing_period", billing_period)
        .execute()
        .data
        or []
    )
    total_cents = sum(int(r.get("amount_cents") or 0) for r in usage_rows)
    line_items: list[dict[str, Any]] = [
        {
            "event_type": r["event_type"],
            "amount_cents": r["amount_cents"],
            "commitment_id": r.get("commitment_id"),
            "usage_id": r["id"],
        }
        for r in usage_rows
    ]

    inv_existing = (
        supabase.table("billing_invoices")
        .select("id")
        .eq("firm_id", firm_id)
        .eq("billing_period", billing_period)
        .execute()
        .data
        or []
    )
    inv_body = {
        "firm_id": firm_id,
        "billing_period": billing_period,
        "status": "draft",
        "total_cents": total_cents,
        "line_items": line_items,
    }
    if inv_existing:
        supabase.table("billing_invoices").update({
            "total_cents": total_cents,
            "line_items": line_items,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", inv_existing[0]["id"]).execute()
        invoice_id = inv_existing[0]["id"]
    else:
        ins = supabase.table("billing_invoices").insert(inv_body).execute()
        invoice_id = ins.data[0]["id"] if ins.data else None

    return {
        "billing_period": billing_period,
        "firm_id": firm_id,
        "onboarding_rows_created": onboarding_created,
        "aip_amount_cents": aip_cents,
        "total_funded": str(total_funded),
        "invoice_total_cents": total_cents,
        "invoice_id": invoice_id,
    }
