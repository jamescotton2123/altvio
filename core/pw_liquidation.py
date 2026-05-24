"""
Private-wealth (Schwab / custody) liquidation coordination.

Uses investors.schwab_estimated_liquid_cash (manual) vs **total investor wire due**
(`compute_commitment_wire_breakdown` → commitment + placement / implementation in wire)
to set liquidation_needed and cash_shortfall. No live broker API — ops/CA updates cash.
"""

from __future__ import annotations

from typing import Any

from core.database import supabase
from core.deal_fees import compute_commitment_wire_breakdown


def get_commitment_total_wire_due(
    committed_amount: float | None,
    deal_id: str,
    firm_id: str,
) -> float:
    """Total wire the investor must fund (subscription + investor-paid fees in wire)."""
    br = compute_commitment_wire_breakdown(
        committed_amount=float(committed_amount or 0),
        deal_id=deal_id,
        firm_id=firm_id,
    )
    return float(br.get("total_wire_due") or 0)


def compute_pw_liquidation_state(
    total_wire_due: float | None,
    schwab_estimated_liquid_cash: float | None,
) -> dict[str, Any]:
    """
    If Schwab cash is unknown (NULL): liquidation_needed True, cash_shortfall NULL (conservative).
    If known: cash_shortfall = max(0, total_wire_due - cash); liquidation_needed = shortfall > 0.
    """
    wire = float(total_wire_due or 0)
    if schwab_estimated_liquid_cash is None:
        return {
            "liquidation_needed": True,
            "cash_shortfall": None,
        }
    cash = float(schwab_estimated_liquid_cash)
    shortfall = max(0.0, wire - cash)
    return {
        "liquidation_needed": shortfall > 0,
        "cash_shortfall": round(shortfall, 2),
    }


def apply_pw_liquidation_on_new_commitment(
    firm_id: str,
    commitment_id: str,
    committed_amount: float,
    investor: dict,
    deal: dict,
    settings: dict,
    *,
    send_alerts: bool = True,
) -> dict[str, Any]:
    """
    For private_wealth investors: set liquidation_required, liquidation_needed, cash_shortfall;
    optionally email CA, ops, assigned trader.
    """
    if not investor.get("private_wealth"):
        return {"applied": False, "reason": "not_private_wealth"}

    deal_id = deal["id"]
    wire_due = get_commitment_total_wire_due(committed_amount, deal_id, firm_id)
    st = compute_pw_liquidation_state(
        wire_due,
        investor.get("schwab_estimated_liquid_cash"),
    )
    supabase.table("commitments").update({
        "liquidation_required": True,
        "liquidation_needed": st["liquidation_needed"],
        "cash_shortfall": st["cash_shortfall"],
    }).eq("id", commitment_id).eq("firm_id", firm_id).execute()

    c = (
        supabase.table("commitments")
        .select("id, deal_id, trader_id, liquidation_needed, cash_shortfall, committed_amount")
        .eq("id", commitment_id)
        .single()
        .execute()
        .data
    )

    if send_alerts and c:
        c = {**c, "total_wire_due": wire_due}
        send_immediate_pw_liquidation_alerts(
            firm_id=firm_id,
            settings=settings,
            commitment=c,
            deal=deal,
            investor=investor,
        )

    return {"applied": True, **st, "commitment_id": commitment_id, "total_wire_due": wire_due}


def send_immediate_pw_liquidation_alerts(
    firm_id: str,
    settings: dict,
    commitment: dict,
    deal: dict,
    investor: dict,
) -> None:
    from core.graph_client import send_email

    cash = investor.get("schwab_estimated_liquid_cash")
    cash_line = f"${float(cash):,.2f}" if cash is not None else "not on file (assume review / possible sells)"
    shortfall = commitment.get("cash_shortfall")
    shortfall_line = (
        f"${float(shortfall):,.2f}" if shortfall is not None else "unknown until cash estimate is updated"
    )
    ln = commitment.get("liquidation_needed")
    tw = float(commitment.get("total_wire_due") or 0)
    if tw <= 0 and commitment.get("deal_id") and commitment.get("committed_amount") is not None:
        tw = get_commitment_total_wire_due(
            commitment.get("committed_amount"),
            commitment["deal_id"],
            firm_id,
        )

    body = (
        "New private-wealth commitment — custody funding coordination (Schwab / public-markets sleeve).\n\n"
        f"Client: {investor.get('entity_name')}\n"
        f"Fund: {deal.get('offering_name')}\n"
        f"Subscription commitment: ${float(commitment.get('committed_amount') or 0):,.2f}\n"
        f"Total wire due (commitment + fees in wire): ${tw:,.2f}\n"
        f"Schwab estimated liquid cash (manual): {cash_line}\n"
        f"Liquidation / trade likely needed: {'Yes' if ln else 'No'}\n"
        f"Cash shortfall vs total wire: {shortfall_line}\n\n"
        f"Commitment ID: {commitment['id']}\n\n"
        "Client Associates: when cash is available, initiate outbound wire per firm process. "
        "Trading: sell to cover if shortfall applies. "
        "Update investors.schwab_estimated_liquid_cash when balances change.\n"
    )

    ca = (investor.get("client_associate_email") or "").strip().lower()
    ops = (settings.get("ops_mailbox") or "").strip()

    if ca:
        send_email(
            settings=settings,
            to=ca,
            cc=[ops] if ops else [],
            subject=f"[Altvio] PW funding — {investor.get('entity_name')} → {deal.get('offering_name')}",
            body=body,
        )
    elif ops:
        send_email(
            settings=settings,
            to=ops,
            cc=[],
            subject=f"[Altvio] PW funding — {investor.get('entity_name')} → {deal.get('offering_name')} (no CA email)",
            body=body,
        )

    tid = commitment.get("trader_id")
    if tid:
        tr = (
            supabase.table("traders")
            .select("email, display_name")
            .eq("id", tid)
            .eq("firm_id", firm_id)
            .single()
            .execute()
            .data
        )
        if tr and tr.get("email"):
            send_email(
                settings=settings,
                to=tr["email"],
                cc=[ops] if ops else [],
                subject=f"[Altvio] PW funding ticket — {investor.get('entity_name')}",
                body=body,
            )


def recompute_commitment_pw_liquidation(commitment_id: str, firm_id: str) -> None:
    """Recompute liquidation_needed / cash_shortfall for one commitment (PW + liquidation_required only)."""
    c = (
        supabase.table("commitments")
        .select(
            "id, deal_id, committed_amount, liquidation_required, investors(private_wealth, schwab_estimated_liquid_cash)"
        )
        .eq("id", commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not c:
        return
    if not c.get("liquidation_required"):
        supabase.table("commitments").update({
            "liquidation_needed": None,
            "cash_shortfall": None,
        }).eq("id", commitment_id).eq("firm_id", firm_id).execute()
        return
    inv = c.get("investors") or {}
    if not inv.get("private_wealth"):
        supabase.table("commitments").update({
            "liquidation_needed": None,
            "cash_shortfall": None,
        }).eq("id", commitment_id).eq("firm_id", firm_id).execute()
        return
    wire_due = get_commitment_total_wire_due(
        c.get("committed_amount"),
        c["deal_id"],
        firm_id,
    )
    st = compute_pw_liquidation_state(
        wire_due,
        inv.get("schwab_estimated_liquid_cash"),
    )
    supabase.table("commitments").update({
        "liquidation_needed": st["liquidation_needed"],
        "cash_shortfall": st["cash_shortfall"],
    }).eq("id", commitment_id).eq("firm_id", firm_id).execute()


def refresh_pw_liquidation_for_investor(investor_id: str, firm_id: str) -> None:
    """After Schwab cash changes: update all active liquidation-flagged commitments for this investor."""
    rows = (
        supabase.table("commitments")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .eq("liquidation_required", True)
        .execute()
        .data
    ) or []
    for r in rows:
        recompute_commitment_pw_liquidation(r["id"], firm_id)


def refresh_pw_liquidation_for_deal(deal_id: str, firm_id: str) -> None:
    """After deal fee arrangements change, recompute PW liquidation fields for flagged commitments."""
    rows = (
        supabase.table("commitments")
        .select("id")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .eq("liquidation_required", True)
        .execute()
        .data
    ) or []
    for r in rows:
        recompute_commitment_pw_liquidation(r["id"], firm_id)
