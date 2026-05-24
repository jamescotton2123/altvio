"""
Pre-close readiness checks for a deal (pure DB rules, no AI).
Used by GET /deals/{id}/readiness and POST /deals/{id}/close.
"""

from __future__ import annotations

from collections import defaultdict

from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.funding_source import funding_source_hub_flags


def _unwrap_investor(inv_raw: dict | list | None) -> dict:
    if not inv_raw:
        return {}
    if isinstance(inv_raw, list):
        return inv_raw[0] if inv_raw else {}
    return inv_raw


def check_deal_readiness(deal_id: str, firm_id: str) -> dict:
    """
    Returns:
    {
        "deal_readiness_score": int (0-100),
        "ready_to_close": bool,
        "blocking_count": int,
        "investor_gaps": [
            {"investor_id": str, "entity_name": str, "missing": list[str]},
            ...
        ],
    }
    """
    deal_row = (
        supabase.table("deals")
        .select("id")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .limit(1)
        .execute()
        .data
    )
    if not deal_row:
        return None

    rows = (
        supabase.table("commitments")
        .select(
            "investor_id, docusign_status, wire_status, "
            "funding_entity_name, funding_entity_matches_kyc, funding_entity_kyc_status, "
            "investors(entity_name, kyc_status, wire_instructions)"
        )
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) or []

    if not rows:
        return {
            "deal_readiness_score": 0,
            "ready_to_close": False,
            "blocking_count": 1,
            "investor_gaps": [],
        }

    by_investor: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        iid = r.get("investor_id")
        if not iid:
            continue
        by_investor[str(iid)].append(r)

    investor_ids = list(by_investor.keys())
    pending_rows = (
        supabase.table("investor_pending_changes")
        .select("investor_id")
        .in_("investor_id", investor_ids)
        .eq("firm_id", firm_id)
        .eq("status", "Pending")
        .execute()
        .data
    ) or []
    pending_investors = {str(p["investor_id"]) for p in pending_rows if p.get("investor_id")}

    investor_gaps: list[dict] = []
    for inv_id, crows in by_investor.items():
        inv = _unwrap_investor(crows[0].get("investors"))
        entity_name = inv.get("entity_name") or "Unknown investor"
        missing: list[str] = []

        for c in crows:
            if (c.get("docusign_status") or "") not in SIGNED_STATES:
                if "Sub docs not signed" not in missing:
                    missing.append("Sub docs not signed")
            if (c.get("wire_status") or "") != "Funded":
                if "Wire not funded" not in missing:
                    missing.append("Wire not funded")

        if (inv.get("kyc_status") or "") != "Approved":
            missing.append("KYC not approved")

        for c in crows:
            flags = funding_source_hub_flags(
                subscriber_entity_name=entity_name,
                funding_entity_name=c.get("funding_entity_name"),
                funding_entity_matches_kyc=c.get("funding_entity_matches_kyc"),
                funding_entity_kyc_status=c.get("funding_entity_kyc_status"),
                wire_status=c.get("wire_status"),
            )
            if flags["funding_source_action_needed"]:
                if not flags["inbound_funding_source_recorded"]:
                    if "Inbound funding entity not recorded" not in missing:
                        missing.append("Inbound funding entity not recorded")
                if flags["funding_entity_kyc_required"]:
                    if "KYC required for funding entity (inbound wire)" not in missing:
                        missing.append("KYC required for funding entity (inbound wire)")

        if inv_id in pending_investors:
            missing.append("Pending investor data changes")

        investor_gaps.append({
            "investor_id": inv_id,
            "entity_name": entity_name,
            "missing": missing,
        })

    total = len(investor_gaps)
    zero_gap = sum(1 for g in investor_gaps if not g["missing"])
    score = int(round(100.0 * zero_gap / total)) if total else 100
    blocking_count = sum(len(g["missing"]) for g in investor_gaps)
    ready_to_close = blocking_count == 0

    return {
        "deal_readiness_score": score,
        "ready_to_close": ready_to_close,
        "blocking_count": blocking_count,
        "investor_gaps": investor_gaps,
    }
