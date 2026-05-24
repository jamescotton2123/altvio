"""
Private-wealth / trader desk — liquidation funding queue and optional daily digest.

Only investors with investors.private_wealth = true (public-markets / Schwab-style custody clients)
appear in trader queues, ops liquidation watch, and digests. Other clients are out of scope here.

Notifications:
  - Trader desk: assigned trader_id + API key (existing).
  - Client Associate: investors.client_associate_email gets a firm digest of their PW clients’
    open liquidation tickets (Schwab wire initiators), when digest is enabled.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from core.database import supabase
from core.pw_liquidation import get_commitment_total_wire_due

DEFAULT_ALERT_DAYS = 14
logger = logging.getLogger(__name__)


def _not_yet_funded(row: dict) -> bool:
    ws = (row.get("wire_status") or "").strip()
    if ws == "Funded":
        funded = float(row.get("funded_amount") or 0)
        return funded <= 0
    return True


def _is_private_wealth_row(row: dict) -> bool:
    inv = row.get("investors") or {}
    return bool(inv.get("private_wealth"))


def _due_window_ok(due_raw: str | None, today: date, warn_days: int | None) -> tuple[bool, int | None]:
    """Returns (include_row, days_until_due). If warn_days is None, no upper bound on due date."""
    days_until: int | None = None
    if due_raw:
        exp = date.fromisoformat(due_raw) if isinstance(due_raw, str) else due_raw
        days_until = (exp - today).days
        if warn_days is not None and days_until > int(warn_days):
            return False, days_until
    elif warn_days is not None:
        # No due date: still include (desk / CA must see ticket)
        pass
    return True, days_until


def _row_to_item(
    r: dict,
    days_until: int | None,
) -> dict[str, Any]:
    inv = r.get("investors") or {}
    deal = r.get("deals") or {}
    return {
        "commitment_id": r["id"],
        "entity_name": inv.get("entity_name"),
        "offering_name": deal.get("offering_name"),
        "deal_status": deal.get("status"),
        "handle_with_care": inv.get("handle_with_care"),
        "private_wealth": inv.get("private_wealth"),
        "client_associate_email": inv.get("client_associate_email"),
        "committed_amount": r.get("committed_amount"),
        "wire_status": r.get("wire_status"),
        "docusign_status": r.get("docusign_status"),
        "liquidation_due_date": r.get("liquidation_due_date"),
        "days_until_due": days_until,
        "liquidation_desk_notes": r.get("liquidation_desk_notes"),
        "liquidation_acknowledged_at": r.get("liquidation_acknowledged_at"),
        "liquidation_needed": r.get("liquidation_needed"),
        "cash_shortfall": r.get("cash_shortfall"),
        "schwab_estimated_liquid_cash": inv.get("schwab_estimated_liquid_cash"),
    }


def _sort_liquidation_items(out: list[dict]) -> None:
    def sort_key(x: dict) -> tuple:
        due = x.get("liquidation_due_date")
        return (
            0 if x.get("handle_with_care") else 1,
            due is None,
            due or "9999-12-31",
        )

    out.sort(key=sort_key)


_COMMITTMENT_LIQUIDATION_SELECT = (
    "id, deal_id, trader_id, committed_amount, funded_amount, wire_status, docusign_status, "
    "liquidation_due_date, liquidation_desk_notes, liquidation_acknowledged_at, created_at, "
    "liquidation_needed, cash_shortfall, "
    "investors(entity_name, handle_with_care, private_wealth, client_associate_email, schwab_estimated_liquid_cash), "
    "deals(offering_name, status)"
)


def _fetch_liquidation_commitment_rows(firm_id: str, trader_id: str | None = None) -> list[dict]:
    q = (
        supabase.table("commitments")
        .select(_COMMITTMENT_LIQUIDATION_SELECT)
        .eq("firm_id", firm_id)
        .eq("liquidation_required", True)
    )
    if trader_id is not None:
        q = q.eq("trader_id", trader_id)
    return q.execute().data or []


def list_liquidation_commitments_for_trader(
    firm_id: str,
    trader_id: str,
    warn_days: int | None = None,
) -> list[dict[str, Any]]:
    """
    Active liquidation tickets for this trader desk — private-wealth clients only.
    If warn_days is set, only rows in that due-date window (or no due date).
    """
    rows = _fetch_liquidation_commitment_rows(firm_id, trader_id)
    today = date.today()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not _is_private_wealth_row(r) or not _not_yet_funded(r):
            continue
        ok, days_until = _due_window_ok(r.get("liquidation_due_date"), today, warn_days)
        if not ok:
            continue
        item = _row_to_item(r, days_until)
        item["total_wire_due"] = get_commitment_total_wire_due(
            r.get("committed_amount"),
            r["deal_id"],
            firm_id,
        )
        out.append(item)

    _sort_liquidation_items(out)
    return out


def list_firm_liquidation_watch(firm_id: str, warn_days: int) -> list[dict[str, Any]]:
    """Ops dashboard: PW-only, unfunded, liquidation-required, in the alert window."""
    rows = _fetch_liquidation_commitment_rows(firm_id, None)
    trader_ids = list({r["trader_id"] for r in rows if r.get("trader_id")})
    tmap: dict[str, dict] = {}
    if trader_ids:
        trs = (
            supabase.table("traders")
            .select("id, display_name, email")
            .in_("id", trader_ids)
            .execute()
            .data
        ) or []
        tmap = {str(t["id"]): t for t in trs}

    today = date.today()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not _is_private_wealth_row(r) or not _not_yet_funded(r):
            continue
        ok, days_until = _due_window_ok(r.get("liquidation_due_date"), today, warn_days)
        if not ok:
            continue
        inv = r.get("investors") or {}
        deal = r.get("deals") or {}
        tid = str(r["trader_id"]) if r.get("trader_id") else None
        tr = tmap.get(tid) if tid else None
        tw = get_commitment_total_wire_due(r.get("committed_amount"), r["deal_id"], firm_id)
        out.append(
            {
                "commitment_id": r["id"],
                "trader_id": r.get("trader_id"),
                "trader_name": (tr or {}).get("display_name"),
                "trader_email": (tr or {}).get("email"),
                "entity_name": inv.get("entity_name"),
                "offering_name": deal.get("offering_name"),
                "handle_with_care": inv.get("handle_with_care"),
                "private_wealth": inv.get("private_wealth"),
                "client_associate_email": inv.get("client_associate_email"),
                "committed_amount": r.get("committed_amount"),
                "total_wire_due": tw,
                "wire_status": r.get("wire_status"),
                "docusign_status": r.get("docusign_status"),
                "liquidation_due_date": r.get("liquidation_due_date"),
                "days_until_due": days_until,
                "liquidation_desk_notes": r.get("liquidation_desk_notes"),
                "liquidation_acknowledged_at": r.get("liquidation_acknowledged_at"),
                "liquidation_needed": r.get("liquidation_needed"),
                "cash_shortfall": r.get("cash_shortfall"),
                "schwab_estimated_liquid_cash": inv.get("schwab_estimated_liquid_cash"),
            }
        )

    out.sort(
        key=lambda x: (
            0 if x.get("handle_with_care") else 1,
            x.get("days_until_due") if x.get("days_until_due") is not None else 10_000,
        )
    )
    return out


def _digest_line_for_item(a: dict, *, include_desk_hint: bool = False) -> str:
    ent = a.get("entity_name") or "Client"
    fund = a.get("offering_name") or "Fund"
    due = a.get("liquidation_due_date") or "TBD"
    du = a.get("days_until_due")
    if du is None:
        status = "due date TBD"
    elif du < 0:
        status = f"OVERDUE ({-du}d)"
    elif du == 0:
        status = "DUE TODAY"
    else:
        status = f"{du} days to due date"
    tw = a.get("total_wire_due")
    cmt = float(a.get("committed_amount") or 0)
    if tw is not None and abs(float(tw) - cmt) > 0.01:
        amt_str = f"total wire ${float(tw):,.0f} (commit ${cmt:,.0f})"
    elif tw is not None:
        amt_str = f"total wire ${float(tw):,.0f}"
    else:
        amt_str = f"${cmt:,.0f}"
    line = f"  • {ent} — {fund} — {amt_str} — target {due} ({status})"
    if a.get("liquidation_needed") is True:
        cs = a.get("cash_shortfall")
        if cs is not None:
            line += f" — sell/shortfall vs wire ~${float(cs):,.0f}"
        else:
            line += " — cash TBD (review)"
    elif a.get("liquidation_needed") is False:
        line += " — cash OK vs est."
    if include_desk_hint and a.get("trader_desk"):
        line += f" [desk: {a['trader_desk']}]"
    return line


def send_client_associate_liquidation_digests(firm_id: str, settings: dict, warn_days: int) -> dict[str, Any]:
    """
    One email per Client Associate (client_associate_email) with PW liquidation tickets in window.
    CAs initiate Schwab wires for these households once proceeds are available.
    """
    from core.graph_client import send_email

    rows = _fetch_liquidation_commitment_rows(firm_id, None)
    trader_ids = list({r["trader_id"] for r in rows if r.get("trader_id")})
    tmap: dict[str, dict] = {}
    if trader_ids:
        trs = (
            supabase.table("traders")
            .select("id, display_name")
            .in_("id", trader_ids)
            .execute()
            .data
        ) or []
        tmap = {str(t["id"]): t for t in trs}

    today = date.today()
    by_ca: dict[str, list[dict]] = {}
    for r in rows:
        if not _is_private_wealth_row(r) or not _not_yet_funded(r):
            continue
        ok, days_until = _due_window_ok(r.get("liquidation_due_date"), today, warn_days)
        if not ok:
            continue
        inv = r.get("investors") or {}
        ca = (inv.get("client_associate_email") or "").strip().lower()
        if not ca:
            continue
        item = _row_to_item(r, days_until)
        item["total_wire_due"] = get_commitment_total_wire_due(
            r.get("committed_amount"),
            r["deal_id"],
            firm_id,
        )
        tid = str(r["trader_id"]) if r.get("trader_id") else None
        tr = tmap.get(tid) if tid else None
        if tr:
            item["trader_desk"] = tr.get("display_name") or ""
        by_ca.setdefault(ca, []).append(item)

    ca_sent = 0
    for ca_email, items in by_ca.items():
        if not items:
            continue
        lines = [_digest_line_for_item(a, include_desk_hint=True) for a in items]
        body = (
            "Private wealth — open liquidation funding tickets (Schwab / custody proceeds → alt investment wire).\n"
            f"When positions are liquidated and cash is available, initiate or queue the outbound wire per firm process.\n\n"
            f"{len(items)} ticket(s), due within {warn_days} days (or no due date set):\n\n"
            + "\n".join(lines)
            + "\n\nThis list includes only clients where private_wealth is true on the investor record.\n"
            "Live hub: GET /client-associate/pw-deals and GET /deals/{deal_id}/hub?role=client_associate with X-CA-Key (ops provisions POST /client-associate/desk + generate-api-key).\n"
        )
        send_email(
            settings=settings,
            to=ca_email,
            cc=[],
            subject=f"[Altvio] PW liquidation / wire queue ({len(items)}) — Client Associate",
            body=body,
        )
        ca_sent += 1
        logger.info("Trader CA digest sent to %s for %s ticket(s).", ca_email, len(items))

    return {"client_associates_emailed": ca_sent, "ca_recipients": list(by_ca.keys())}


def send_trader_liquidation_digest(firm_id: str, settings: dict) -> dict[str, Any]:
    """
    Email each desk their private-wealth liquidation queue for this firm, then Client Associates.
    Requires notify_trader_liquidation_digest. Desks need api_keys; CAs need client_associate_email on investors
    and a client_associates row + api_key for GET /deals/{id}/hub?role=client_associate (X-CA-Key).
    """
    from core.graph_client import send_email

    if not settings.get("notify_trader_liquidation_digest"):
        return {"sent": False, "reason": "notify_disabled"}

    warn_days = int(settings.get("trader_liquidation_alert_days") or DEFAULT_ALERT_DAYS)

    traders = (
        supabase.table("traders")
        .select("id, email, display_name")
        .eq("firm_id", firm_id)
        .eq("is_active", True)
        .not_.is_("api_key", "null")
        .execute()
        .data
    ) or []

    sent = 0
    for t in traders:
        items = list_liquidation_commitments_for_trader(firm_id, t["id"], warn_days=warn_days)
        if not items:
            continue
        lines = [_digest_line_for_item(a) for a in items]
        body = (
            "Private wealth clients only — liquidation funding queue "
            f"({len(items)} open ticket(s); due within {warn_days} days or no date set):\n\n"
            + "\n".join(lines)
            + "\n\nCall GET /trader/liquidations with your desk API key for live data.\n"
        )
        send_email(
            settings=settings,
            to=t["email"],
            cc=[],
            subject=f"[Altvio] PW liquidation queue ({len(items)}) — {t.get('display_name') or 'Desk'}",
            body=body,
        )
        sent += 1
        logger.info("Trader digest sent to %s for %s ticket(s).", t["email"], len(items))

    ca_summary = send_client_associate_liquidation_digests(firm_id, settings, warn_days)

    if sent == 0 and ca_summary.get("client_associates_emailed", 0) == 0:
        return {
            "sent": False,
            "reason": "no_pw_liquidation_tickets_in_window",
            "desks_emailed": 0,
            "warn_days": warn_days,
            **ca_summary,
        }

    return {
        "sent": sent > 0 or ca_summary.get("client_associates_emailed", 0) > 0,
        "desks_emailed": sent,
        "warn_days": warn_days,
        **ca_summary,
    }


def send_all_firm_trader_digests() -> None:
    """08:00 job: iterate active firms and send trader digests where enabled."""
    firms = supabase.table("firms").select("id").eq("status", "active").execute().data or []
    for row in firms:
        firm_id = row["id"]
        settings = (
            supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute().data
        )
        if not settings:
            continue
        try:
            send_trader_liquidation_digest(firm_id, settings)
        except Exception as e:
            logger.error("Trader digest failed for firm %s: %s", firm_id, e)
