"""
Email-based investor lookup for intake automation.
Returns all entity records tied to an email (one person may have IRA, trust, LLC, etc.).
"""

from __future__ import annotations

from typing import Optional

from core.database import supabase

LOOKUP_EMAIL_FIELDS = (
    "primary_email",
    "advisor_email",
    "client_one_email",
    "client_two_email",
)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def lookup_investors_by_email(firm_id: str, email: str) -> list[dict]:
    """
    Find investors whose contact emails match (case-insensitive).
    Returns enriched rows with last_commitment_date when available.
    """
    needle = normalize_email(email)
    if not needle or "@" not in needle:
        return []

    or_filter = ",".join(f"{field}.ilike.{needle}" for field in LOOKUP_EMAIL_FIELDS)
    result = (
        supabase.table("investors")
        .select(
            "id, entity_name, entity_type, primary_email, advisor_email, "
            "kyc_status, orion_match_status, phone, mailing_address, tax_id, "
            "tax_id_type, country_of_formation, state_of_formation, "
            "date_of_birth, accredited_investor, qualified_purchaser, "
            "client_one_name, client_one_email, client_one_phone, "
            "client_two_name, client_two_email, client_two_phone, "
            "interested_parties, orion_is_new_household, orion_linked_household_name, "
            "preferred_contact_method, prefers_physical_mail, no_electronic_access, "
            "do_not_contact, internal_notes, handle_with_care, created_at"
        )
        .eq("firm_id", firm_id)
        .or_(or_filter)
        .order("entity_name")
        .execute()
    )
    rows = result.data or []
    if not rows:
        return []

    investor_ids = [r["id"] for r in rows]
    commitments = (
        supabase.table("commitments")
        .select("investor_id, created_at")
        .eq("firm_id", firm_id)
        .in_("investor_id", investor_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    last_commitment: dict[str, str] = {}
    for c in commitments:
        inv_id = c.get("investor_id")
        if inv_id and inv_id not in last_commitment:
            last_commitment[inv_id] = c.get("created_at")

    enriched: list[dict] = []
    seen_ids: set[str] = set()
    for row in rows:
        inv_id = row["id"]
        if inv_id in seen_ids:
            continue
        seen_ids.add(inv_id)
        emails_matched = [
            field
            for field in LOOKUP_EMAIL_FIELDS
            if normalize_email(str(row.get(field) or "")) == needle
        ]
        enriched.append(
            {
                **row,
                "last_commitment_date": last_commitment.get(inv_id),
                "matched_on": emails_matched,
            }
        )
    return enriched


def pick_investor_for_email_intake(
    firm_id: str,
    email: Optional[str],
    entity_name: Optional[str] = None,
) -> Optional[dict]:
    """
    When parsing an inbound email, prefer an existing investor if email matches.
    If multiple entities share the email, pick exact entity_name match when possible.
    """
    if not email or not str(email).strip():
        return None
    matches = lookup_investors_by_email(firm_id, email)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if entity_name:
        name_key = entity_name.strip().lower()
        for m in matches:
            if (m.get("entity_name") or "").strip().lower() == name_key:
                return m
    return None
