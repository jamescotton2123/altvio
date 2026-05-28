"""
Third-party / placement fee economics tied to deals (`deal_fee_arrangements`).

Used to compute **investor wire amounts** (commitment + investor-paid placement items).
Carry is disclosed for documents and Orion planning but never added to subscription wire.

Orion booking (high level — firm configures detail in Orion; NAImport is investor-centric):
  - **Investor subscription wire**: Typically booked to fund capital / purchase (commitment +
    any placement fee the LPA treats as additional capital or separate payables).
  - **Placement / upfront fees paid by investor**: Often coded as fund offering cost, capitalized,
    or offset against capital — follow your fund counsel and Orion chart of accounts. Map as a
    separate cash receipt line or payable if the investor wires one aggregate; use fee
    breakdown line items as memo text in the transaction description.
  - **Implementation fee (one-time)**: Usually a fund expense or prepaid asset; if
    `include_implementation_in_wire` is True, the per-investor share is part of the wire and
    should mirror how the LPA characterizes that payment (often alongside placement).
  - **Carried interest**: Not part of initial investor funding in this module. In Orion, carry is
    profit allocation over life of fund — track economically outside NAImport templates; use GP /
    carry partner capital accounts per your admin policy.

This module assumes an **additive wire model**: total_wire = commitment + sum(investor-paid fees).
If your governing docs use net-of-fee funding, override amounts in comms or extend with a
`wire_model` flag later.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.database import supabase

UPFRONT_AMOUNT_BASIS = frozenset({"per_commitment", "pro_rata_deal_total"})
MONEY_QUANT = Decimal("0.01")


def fetch_deal_fee_arrangements(deal_id: str, firm_id: str) -> list[dict]:
    return (
        supabase.table("deal_fee_arrangements")
        .select("*")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .order("created_at")
        .execute()
        .data
    ) or []


def active_commitments_capital(deal_id: str, firm_id: str) -> tuple[Decimal, int]:
    """Sum of committed_amount and row count for Active commitments on the deal."""
    rows = (
        supabase.table("commitments")
        .select("committed_amount")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) or []
    total = sum(Decimal(str(r.get("committed_amount") or 0)) for r in rows)
    return total, len(rows)


def _q2(x: Decimal | float | int) -> float:
    return float(Decimal(str(x)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))


def _party_label(arr: dict) -> str:
    name = (arr.get("recipient_name") or "").strip()
    if name:
        return name
    t = arr.get("arrangement_type") or "third_party"
    return str(t).replace("_", " ").title()


def compute_commitment_wire_breakdown(
    *,
    committed_amount: float,
    deal_id: str,
    firm_id: str,
) -> dict[str, Any]:
    """
    Build line items and totals for what this investor should wire (gross of commitment).

    Precedence per arrangement row:
      - If `upfront_fee_pct` is set: pct of *this commitment* (ignores flat amount on same row).
      - Else if `upfront_fee_amount` is set: uses `upfront_fee_amount_basis`
          (per_commitment | pro_rata_deal_total).
      - `implementation_fee` with `include_implementation_in_wire`: equal share across all
        active commitments on the deal (recomputed as investors join — ops should resend wire comms
        if counts change materially).
      - Carry fields: disclosure only.
    """
    cmt = Decimal(str(committed_amount or 0))
    arrangements = fetch_deal_fee_arrangements(deal_id, firm_id)
    deal_total, n_commit = active_commitments_capital(deal_id, firm_id)

    lines: list[dict[str, Any]] = [
        {
            "code": "commitment",
            "label": "Subscription commitment",
            "amount": _q2(cmt),
        }
    ]
    warnings: list[str] = []
    carry_notes: list[dict[str, Any]] = []

    for arr in arrangements:
        label = _party_label(arr)
        impl = arr.get("implementation_fee")
        inc_impl = bool(arr.get("include_implementation_in_wire"))
        pct = arr.get("upfront_fee_pct")
        flat = arr.get("upfront_fee_amount")
        basis = (arr.get("upfront_fee_amount_basis") or "per_commitment").strip()

        if basis not in UPFRONT_AMOUNT_BASIS:
            basis = "per_commitment"
            warnings.append(f"Invalid upfront_fee_amount_basis on arrangement {arr.get('id')}; used per_commitment.")

        upfront_piece = Decimal(0)
        if pct is not None:
            upfront_piece = cmt * (Decimal(str(pct)) / Decimal(100))
            lines.append({
                "code": "upfront_pct",
                "arrangement_id": arr.get("id"),
                "label": f"Upfront / placement ({label}) — {pct}% of commitment",
                "amount": _q2(upfront_piece),
            })
        elif flat is not None:
            flat_d = Decimal(str(flat))
            if basis == "per_commitment":
                upfront_piece = flat_d
                lines.append({
                    "code": "upfront_flat",
                    "arrangement_id": arr.get("id"),
                    "label": f"Upfront / placement ({label}) — flat per investor",
                    "amount": _q2(upfront_piece),
                })
            else:
                if deal_total <= 0:
                    upfront_piece = Decimal(0)
                    warnings.append(
                        f"Pro-rata upfront for {label} is $0 — no active commitments on deal to allocate against."
                    )
                else:
                    upfront_piece = flat_d * (cmt / deal_total)
                    lines.append({
                        "code": "upfront_pro_rata",
                        "arrangement_id": arr.get("id"),
                        "label": f"Upfront / placement ({label}) — your share of deal total",
                        "amount": _q2(upfront_piece),
                    })

        if inc_impl and impl is not None:
            impl_d = Decimal(str(impl))
            if n_commit <= 0:
                warnings.append(f"Implementation fee split for {label} is $0 — no active commitments.")
                share = Decimal(0)
            else:
                share = impl_d / Decimal(n_commit)
            lines.append({
                "code": "implementation_share",
                "arrangement_id": arr.get("id"),
                "label": f"Implementation fee share ({label}) — {n_commit} active slot(s)",
                "amount": _q2(share),
            })

        cp = arr.get("carry_pct")
        ch = arr.get("carry_hurdle_pct")
        if cp is not None or ch is not None:
            carry_notes.append({
                "arrangement_id": arr.get("id"),
                "party": label,
                "carry_pct": float(cp) if cp is not None else None,
                "carry_hurdle_pct": float(ch) if ch is not None else None,
                "note": "Carried interest does not increase today's wire — book per fund docs in Orion (GP/carry accounts).",
            })

    fee_component_total = sum(
        (Decimal(str(x["amount"])) for x in lines if x["code"] != "commitment"),
        Decimal(0),
    )
    total_wire = cmt + fee_component_total

    return {
        "committed_amount": _q2(cmt),
        "third_party_fees_total": _q2(fee_component_total),
        "total_wire_due": _q2(total_wire),
        "lines": lines,
        "carry_disclosures": carry_notes,
        "warnings": warnings,
        "deal_active_commitment_count": n_commit,
        "deal_total_committed": _q2(deal_total),
        "orion_reminder": (
            "Third-party fees included in investor wires usually need offsetting lines in Orion "
            "(offering cost, payable, or additional paid-in capital per counsel). "
            "NAImport reflects investor capital — map placement lines per your admin convention."
        ),
    }
