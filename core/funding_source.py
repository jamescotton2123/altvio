"""
Inbound subscription funding source vs KYC subscriber entity.

investors.wire_instructions = where the FIRM sends distributions TO the investor (payout).
commitments.funding_entity_name = legal entity the inbound wire was sent FROM.
"""

from __future__ import annotations

import re
from typing import Any

FUNDING_KYC_STATUSES = frozenset({"not_recorded", "not_required", "required", "complete"})


def normalize_entity_name(name: str | None) -> str:
    """Loose match for ops-entered vs CRM entity names."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for suffix in (" llc", " l l c", " lp", " l p", " inc", " corp", " ltd", " trust"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def entities_match(subscriber_name: str | None, funding_name: str | None) -> bool:
    a = normalize_entity_name(subscriber_name)
    b = normalize_entity_name(funding_name)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def build_funding_source_fields(
    subscriber_entity_name: str | None,
    funding_entity_name: str | None,
    *,
    current_kyc_status: str | None = None,
    ops_mark_kyc_complete: bool = False,
) -> dict[str, Any]:
    """
    Compute DB columns when ops records or updates inbound funding entity.
    Preserves funding_entity_kyc_status='complete' unless name changes.
    """
    funding = (funding_entity_name or "").strip() or None
    if not funding:
        return {
            "funding_entity_name": None,
            "funding_entity_matches_kyc": None,
            "funding_entity_kyc_status": "not_recorded",
        }

    if ops_mark_kyc_complete:
        matches = entities_match(subscriber_entity_name, funding)
        return {
            "funding_entity_name": funding,
            "funding_entity_matches_kyc": matches,
            "funding_entity_kyc_status": "complete",
        }

    if current_kyc_status == "complete" and entities_match(subscriber_entity_name, funding):
        return {
            "funding_entity_name": funding,
            "funding_entity_matches_kyc": True,
            "funding_entity_kyc_status": "complete",
        }

    matches = entities_match(subscriber_entity_name, funding)
    if matches:
        status = "not_required"
    else:
        status = "required"

    return {
        "funding_entity_name": funding,
        "funding_entity_matches_kyc": matches,
        "funding_entity_kyc_status": status,
    }


def funding_source_hub_flags(
    subscriber_entity_name: str | None,
    funding_entity_name: str | None,
    funding_entity_matches_kyc: bool | None,
    funding_entity_kyc_status: str | None,
    wire_status: str | None,
) -> dict[str, Any]:
    """Labels and booleans for deal hub / active deal tracker rows."""
    status = (funding_entity_kyc_status or "not_recorded").strip().lower()
    funding = (funding_entity_name or "").strip() or None
    subscriber = (subscriber_entity_name or "").strip() or None

    if status not in FUNDING_KYC_STATUSES:
        status = "not_recorded"

    needs_record = wire_status in ("Funded", "Awaiting Funds") and not funding
    alt_kyc_required = status == "required"
    alt_kyc_complete = status == "complete"

    if funding and funding_entity_matches_kyc is True:
        match_label = "matches_subscriber"
    elif funding and funding_entity_matches_kyc is False:
        match_label = "different_entity"
    elif funding:
        match_label = "unverified"
    else:
        match_label = "not_recorded"

    return {
        "funding_entity_name": funding,
        "subscriber_entity_name": subscriber,
        "funding_entity_matches_kyc": funding_entity_matches_kyc,
        "funding_entity_kyc_status": status,
        "funding_entity_match_label": match_label,
        "inbound_funding_source_recorded": bool(funding),
        "funding_entity_kyc_required": alt_kyc_required,
        "funding_entity_kyc_complete": alt_kyc_complete,
        "funding_source_action_needed": needs_record or alt_kyc_required,
    }
