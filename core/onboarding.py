"""
Central onboarding orchestrator.
Accepts a parsed payload from either ai_parser or form submission
and executes the full investor onboarding chain:
  1. Upsert investor
  2. Find or create deal
  3. Insert commitment
  4. Provision SharePoint KYC folder
  5. Send DocuSign envelope
  6. Send Email 1 (DocuSign dispatch notice)
  7. Send Email 3 (KYC request)
"""

import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)
logger = logging.getLogger(__name__)

from core.audit import log_audit  # noqa: E402
from core.database import supabase  # noqa: E402


def get_firm_settings(firm_id: str) -> dict:
    response = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not response.data:
        raise ValueError(f"No firm_settings found for firm_id={firm_id}")
    return response.data


def upsert_investor(firm_id: str, payload: dict) -> dict:
    """Upsert investor by entity_name within a firm. Returns the investor record."""
    investor_data = {
        "firm_id": firm_id,
        "entity_name": payload["investor_name"],
        "entity_type": payload.get("entity_type"),
        "primary_email": payload.get("investor_email"),
        "advisor_email": payload.get("advisor_email"),
        "kyc_status": "Pending",
    }
    if payload.get("advisor_id"):
        investor_data["advisor_id"] = payload["advisor_id"]
    if payload.get("country_of_formation"):
        investor_data["country_of_formation"] = payload["country_of_formation"]
    # Persist Orion householding declaration from advisor — never overwrite if already set
    if "orion_is_new_household" in payload:
        investor_data["orion_is_new_household"] = payload["orion_is_new_household"]
    if payload.get("orion_linked_household_name"):
        investor_data["orion_linked_household_name"] = payload["orion_linked_household_name"]

    # Identity + Orion NAImport fields supplied at intake
    for field in (
        "tax_id",
        "tax_id_type",
        "mailing_address",
        "phone",
        "date_of_birth",
        "client_one_name", "client_one_email", "client_one_phone", "client_one_dob", "client_one_ssn_last4",
        "client_two_name", "client_two_email", "client_two_phone", "client_two_dob", "client_two_ssn_last4",
        "preferred_contact_method",
        "no_electronic_access",
        "prefers_physical_mail",
        "do_not_contact",
        "accredited_investor",
        "qualified_purchaser",
        "internal_notes",
    ):
        if payload.get(field) is not None:
            investor_data[field] = payload[field]

    if payload.get("interested_parties"):
        investor_data["interested_parties"] = payload["interested_parties"]

    # Wire instructions provided at intake (advisor-supplied)
    if payload.get("wire_instructions"):
        investor_data["wire_instructions"] = payload["wire_instructions"]

    response = (
        supabase.table("investors")
        .upsert(investor_data, on_conflict="entity_name")
        .execute()
    )
    return response.data[0]


def find_or_create_deal(firm_id: str, fund_name: str) -> dict:
    """Find an existing active deal by offering_name, or create a new one."""
    existing = (
        supabase.table("deals")
        .select("*")
        .eq("firm_id", firm_id)
        .eq("offering_name", fund_name)
        .eq("status", "Active")
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]

    new_deal = (
        supabase.table("deals")
        .insert({"firm_id": firm_id, "offering_name": fund_name, "status": "Active"})
        .execute()
    )
    return new_deal.data[0]


def create_commitment(
    firm_id: str,
    investor_id: str,
    deal_id: str,
    committed_amount: float,
    advisory_fee_pct: float = 1.0,
    internal_notes: Optional[str] = None,
    side_letter_requested: bool = False,
    side_letter_terms: Optional[str] = None,
) -> dict:
    """Insert a new commitment record. memorandum_number is auto-assigned by DB trigger."""
    commitment_data = {
        "firm_id": firm_id,
        "investor_id": investor_id,
        "deal_id": deal_id,
        "committed_amount": committed_amount,
        "advisory_fee_pct": advisory_fee_pct,
        "docusign_status": "Pending",
        "wire_status": "Awaiting Funds",
        "status": "Active",
        "side_letter_requested": side_letter_requested,
    }
    if internal_notes:
        commitment_data["internal_notes"] = internal_notes
    if side_letter_terms:
        commitment_data["side_letter_terms"] = side_letter_terms
    response = supabase.table("commitments").insert(commitment_data).execute()
    return response.data[0]


def run_onboarding(firm_id: str, payload: dict) -> dict:
    """
    Full onboarding chain. Payload shape (from ai_parser or form mapper):
    {
        "investor_name": str,
        "fund_name": str,
        "committed_amount": float,
        "advisor_email": str | None,
        "investor_email": str | None,
        "entity_type": str | None,
        "advisor_id": str | None,
        "advisory_fee_pct": float | None,   # defaults via fallback chain
        "orion_is_new_household": bool,
        "orion_linked_household_name": str | None,
        "country_of_formation": str | None,
        "confidence": "high" | "medium" | "low"
    }

    KYC-first flow (Phase 3):
    - New investors: KYC folder + KYC email only. Sub docs sent after KYC is approved.
    - Existing investors (kyc_status == "Approved"): sub docs sent immediately.
    """
    from core.email_templates import build_docusign_dispatch_email
    from core.graph_client import create_kyc_folder, send_email
    from core.kyc_templates import build_kyc_email

    settings = get_firm_settings(firm_id)

    # Step 1 — Upsert investor (reload full row for PW / Schwab flags not in intake payload)
    investor = upsert_investor(firm_id, payload)
    investor_id = investor["id"]
    investor_full = (
        supabase.table("investors").select("*").eq("id", investor_id).single().execute().data or investor
    )
    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="investor.upsert",
        entity_type="investor",
        entity_id=investor_id,
        after=investor_full,
        metadata={"source": "onboarding"},
    )
    logger.info("Investor upserted: %s (%s)", investor_full["entity_name"], investor_id)

    # Orion fuzzy match — skip when advisor declared a new household
    if not payload.get("orion_is_new_household"):
        try:
            from core.orion_matcher import match_investor

            match_investor(
                investor_id=investor_id,
                firm_id=firm_id,
                entity_name=investor_full["entity_name"],
            )
        except Exception as exc:
            logger.warning("Orion match on intake failed for %s: %s", investor_id, exc)

    # Step 2 — Find or create deal
    deal = find_or_create_deal(firm_id, payload["fund_name"])
    deal_id = deal["id"]
    logger.info("Deal resolved: %s (%s)", deal["offering_name"], deal_id)

    # Step 3 — Create commitment (with advisory fee fallback chain)
    advisory_fee_pct = _resolve_advisory_fee(
        advisor_provided=payload.get("advisory_fee_pct"),
        investor=investor_full,
        settings=settings,
    )
    commitment = create_commitment(
        firm_id,
        investor_id,
        deal_id,
        payload["committed_amount"],
        advisory_fee_pct,
        internal_notes=payload.get("internal_notes"),
        side_letter_requested=bool(payload.get("side_letter_requested")),
        side_letter_terms=payload.get("side_letter_terms"),
    )
    commitment_id = commitment["id"]
    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="commitment.create",
        entity_type="commitment",
        entity_id=commitment_id,
        after=commitment,
        metadata={"source": "onboarding", "investor_id": investor_id, "deal_id": deal_id},
    )
    logger.info(
        "Commitment created: $%s (fee=%s%%)",
        f"{payload['committed_amount']:,.2f}",
        advisory_fee_pct,
    )

    from core.pw_liquidation import apply_pw_liquidation_on_new_commitment

    apply_pw_liquidation_on_new_commitment(
        firm_id=firm_id,
        commitment_id=commitment_id,
        committed_amount=float(payload["committed_amount"]),
        investor=investor_full,
        deal=deal,
        settings=settings,
        send_alerts=True,
    )

    # Step 4 — Provision SharePoint KYC folder
    folder_result = create_kyc_folder(
        settings=settings,
        entity_name=investor_full["entity_name"],
        fund_name=deal["offering_name"],
    )
    sharepoint_link = folder_result["sharepoint_link"]
    folder_id = folder_result["folder_id"]

    supabase.table("investors").update({
        "sharepoint_folder_id": folder_id,
        "sharepoint_link": sharepoint_link,
    }).eq("id", investor_id).execute()
    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="sharepoint_folder.create",
        entity_type="investor",
        entity_id=investor_id,
        after={"sharepoint_folder_id": folder_id, "sharepoint_link": sharepoint_link},
        metadata={"source": "onboarding"},
    )
    logger.info("SharePoint folder provisioned: %s", sharepoint_link)

    is_existing_client = investor_full.get("kyc_status") == "Approved"
    envelope_id = None

    if is_existing_client:
        # Existing investor — KYC is already done, send sub docs immediately
        from core.docusign_client import send_envelope
        envelope_result = send_envelope(
            settings=settings,
            investor=investor_full,
            deal=deal,
            commitment=commitment,
        )
        envelope_id = envelope_result["envelope_id"]
        supabase.table("commitments").update({
            "envelope_id": envelope_id,
            "docusign_status": "Sent",
        }).eq("id", commitment_id).execute()
        logger.info("Existing client sub docs sent immediately: %s", envelope_id)

        # Send Email 1: DocuSign dispatch notice
        if investor_full.get("primary_email"):
            email1 = build_docusign_dispatch_email(
                entity_name=investor_full["entity_name"],
                offering_name=deal["offering_name"],
                committed_amount=payload["committed_amount"],
                advisor_name=payload.get("advisor_email"),
                ops_contact_email=settings.get("ops_mailbox"),
                firm_id=firm_id,
            )
            send_email(
                settings=settings,
                to=investor_full["primary_email"],
                cc=[investor_full["advisor_email"]] if investor_full.get("advisor_email") else [],
                subject=email1["subject"],
                body=email1["body"],
            )
            logger.info("Email 1 sent to %s", investor_full["primary_email"])
    else:
        # New investor — send KYC portal link; sub docs triggered after KYC approval
        from core.portal import generate_kyc_token
        kyc_token_result = generate_kyc_token(
            firm_id=firm_id,
            investor_id=investor_id,
            commitment_id=commitment_id,
            settings=settings,
        )
        kyc_upload_url = kyc_token_result["kyc_url"]

        if investor_full.get("primary_email"):
            kyc_email = build_kyc_email(
                entity_name=investor_full["entity_name"],
                entity_type=investor_full.get("entity_type", "Individual"),
                offering_name=deal["offering_name"],
                sharepoint_upload_link=kyc_upload_url,
                ops_contact_email=settings.get("ops_mailbox"),
            )
            send_email(
                settings=settings,
                to=investor_full["primary_email"],
                cc=[investor_full["advisor_email"]] if investor_full.get("advisor_email") else [],
                subject=kyc_email["subject"],
                body=kyc_email["body"],
            )
            logger.info("New client KYC portal link sent: %s. Sub docs pending approval.", kyc_upload_url)

    return {
        "investor_id": investor_id,
        "deal_id": deal_id,
        "commitment_id": commitment_id,
        "envelope_id": envelope_id,
        "sharepoint_link": sharepoint_link,
        "status": "kyc_pending" if not is_existing_client else "onboarding_complete",
        "kyc_first": not is_existing_client,
    }


def _resolve_advisory_fee(
    advisor_provided: float | None,
    investor: dict,
    settings: dict,
) -> float:
    """
    Advisory fee fallback chain:
    1. Advisor-provided value from intake
    2. investors.existing_orion_fee_pct (from Orion on file)
    3. firm_settings.default_advisory_fee_pct (default 1.0)
    """
    if advisor_provided is not None:
        return float(advisor_provided)
    if investor.get("existing_orion_fee_pct"):
        return float(investor["existing_orion_fee_pct"])
    return float(settings.get("default_advisory_fee_pct") or 1.0)
