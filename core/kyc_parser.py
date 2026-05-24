"""
Agentic KYC document reviewer using GPT-4o Vision.
Triggered when a document is uploaded to a SharePoint KYC folder.

Performs:
  1. Match check — does the uploaded doc match the type requested?
  2. Nested entity extraction — are there other LLCs, trusts, individuals listed?
  3. Signatory extraction — who is qualified to sign?
  4. Formation date extraction
  5. Flags if nested entities require additional KYC
  6. Auto-creates investor stubs for nested entities and fires KYC Email 3 for each
  7. Writes high-confidence extracted fields to investors table (Phase 3)
  8. Routes medium-confidence fields to investor_pending_changes for ops review (Phase 3)
  9. Triggers sub doc send when KYC status becomes Approved (Phase 3)
"""

import logging
import time
from typing import Optional

from core.database import supabase

logger = logging.getLogger(__name__)

KYC_REVIEW_PROMPT = """
You are a KYC compliance specialist reviewing an uploaded document for an alternative investment firm.

Analyze the provided document image and return a JSON object with this exact structure:

{
  "document_type_detected": "e.g. Articles of Organization, Trust Agreement, Passport, W-9, Operating Agreement, Org Chart",
  "matches_requested_type": true or false,
  "confidence": "high", "medium", or "low",
  "entity_name": "Primary entity name on the document",
  "formation_date": "YYYY-MM-DD or null",
  "state_of_formation": "State abbreviation or null",
  "ownership_structure": {
    "type": "Single | Joint | LLC | Trust | LP | Corporation | Org Chart | Unknown",
    "is_joint_tenancy": false,
    "joint_tenancy_type": "JT WROS | JTWROS | Joint Tenants | Tenants in Common | null",
    "joint_tenants": [
      {"name": "Full name", "title": "e.g. Joint Tenant"}
    ]
  },
  "nested_entities": [
    {
      "name": "Name of the nested entity",
      "type": "Individual, LLC, Trust, LP, Corporation, or Other",
      "role": "e.g. Member, Manager, Trustee, General Partner, Beneficial Owner",
      "ownership_percentage": null or number,
      "requires_kyc": true or false
    }
  ],
  "signatories": [
    {
      "name": "Full name",
      "title": "e.g. Manager, Trustee, President, Joint Tenant",
      "qualified_to_sign": true or false
    }
  ],
  "flags": [
    "Any issues found — e.g. document is expired, signature is missing, entity name mismatch"
  ],
  "status": "Approved", "Needs More", or "Rejected",
  "escalate_to_compliance": false
}

Rules for nested entity KYC requirements:
- Any beneficial owner with 25% or more ownership requires_kyc = true
- Any trustee or general partner requires_kyc = true
- Any manager of an LLC requires_kyc = true
- Nested entities that are themselves LLCs, trusts, or LPs always require_kyc = true

Rules for joint tenancy:
- If you detect "JT WROS", "JTWROS", "Joint Tenants", or "Tenants in Common" anywhere in the document,
  set ownership_structure.is_joint_tenancy = true and list BOTH tenants in joint_tenants[].
- Both joint tenants are required signatories (qualified_to_sign = true for each).

Rules for org charts:
- If the document is an ownership/org chart, extract all entities in the tree as nested_entities.
- Flag each layer that requires KYC based on the ownership rules above.

Rules for escalate_to_compliance:
- Set escalate_to_compliance = true when confidence = "low" OR when you cannot determine
  whether the investor has uploaded all necessary documents to make a clear determination.
  Better to escalate than to incorrectly approve.
"""


def review_kyc_document(
    file_bytes: bytes,
    *,
    firm_id: str,
    requested_doc_type: Optional[str] = None,
    entity_name: Optional[str] = None,
) -> dict:
    """
    Review an uploaded KYC document using the firm's configured AI engine.
    Returns structured review result.
    """
    from core.ai_engines.registry import get_kyc_reviewer
    from core.audit import log_audit
    from core.database import supabase as _db

    settings = (
        _db.table("firm_settings")
        .select("kyc_engine")
        .eq("firm_id", str(firm_id))
        .single()
        .execute()
        .data
        or {}
    )
    engine_name = settings.get("kyc_engine", "openai_vision")
    reviewer = get_kyc_reviewer(engine_name)
    t0 = time.monotonic()
    status = "ok"
    try:
        result = reviewer.review(
            file_bytes,
            requested_doc_type=requested_doc_type,
            entity_name=entity_name,
        )
        return result
    except Exception:
        status = "error"
        raise
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        audit_log_id = log_audit(
            firm_id=firm_id,
            actor_type="system",
            actor_id=None,
            action="ai_invocation.kyc_review",
            entity_type="ai_invocation",
            metadata={
                "engine": reviewer.name,
                "model_version": reviewer.model_version,
                "task": "kyc_review",
                "status": status,
                "requested_doc_type": requested_doc_type,
                "entity_name": entity_name,
                "latency_ms": latency_ms,
            },
        )
        _db.table("ai_invocations").insert({
            "firm_id": str(firm_id),
            "engine": reviewer.name,
            "model_version": reviewer.model_version,
            "task": "kyc_review",
            "latency_ms": latency_ms,
            "status": status,
            "audit_log_id": audit_log_id,
        }).execute()


def process_kyc_upload(
    investor_id: str,
    firm_id: str,
    filename: str,
    file_bytes: bytes,
    settings: dict,
    source_archive: Optional[str] = None,
    source_doc_url: Optional[str] = None,
) -> dict:
    """
    Full KYC processing flow. Called by the SharePoint webhook handler
    when a file is uploaded to an investor's KYC folder.

    1. Run AI review on the uploaded document.
    2. Write results to kyc_reviews table.
    3. Update investor kyc_status.
    4. For each nested entity requiring KYC:
       a. Upsert a new investor stub.
       b. Provision a SharePoint folder.
       c. Fire KYC Email 3 for the nested entity.
    """
    from core.audit import log_audit
    from core.database import supabase
    from core.graph_client import create_kyc_folder, send_email
    from core.kyc_templates import build_kyc_email

    # Fetch investor record
    investor = (
        supabase.table("investors")
        .select("entity_name, entity_type, advisor_email, primary_email, sharepoint_folder_id, kyc_status")
        .eq("id", investor_id)
        .single()
        .execute()
        .data
    )

    # Run AI review
    review = review_kyc_document(
        file_bytes=file_bytes,
        firm_id=firm_id,
        entity_name=investor["entity_name"],
    )

    escalate = review.get("escalate_to_compliance", False) or review.get("confidence") == "low"

    # Write to kyc_reviews
    kyc_review_payload = {
        "firm_id": firm_id,
        "investor_id": investor_id,
        "matched_docs": [review.get("document_type_detected")],
        "nested_entities": review.get("nested_entities", []),
        "signatories": review.get("signatories", []),
        "formation_date": review.get("formation_date"),
        "flags": review.get("flags", []),
        "status": review.get("status", "Reviewing"),
        "source_archive": source_archive,
        "escalated_to_compliance": escalate,
        "ownership_structure": review.get("ownership_structure"),
    }
    kyc_review = supabase.table("kyc_reviews").insert(kyc_review_payload).execute().data[0]
    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="kyc_review.create",
        entity_type="kyc_review",
        entity_id=kyc_review["id"],
        after=kyc_review,
        metadata={"source": "kyc_parser", "investor_id": investor_id, "filename": filename},
    )

    # Update investor KYC status — escalate overrides status
    new_kyc_status = "Escalated" if escalate else review.get("status", "Reviewing")
    supabase.table("investors").update({"kyc_status": new_kyc_status}).eq(
        "id", investor_id
    ).execute()
    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="investor.kyc_status.update",
        entity_type="investor",
        entity_id=investor_id,
        before={"kyc_status": investor.get("kyc_status")},
        after={"kyc_status": new_kyc_status},
        metadata={"source": "kyc_parser", "filename": filename},
    )
    logger.info("KYC parser status for %s: %s", investor["entity_name"], new_kyc_status)

    # Escalate to compliance if AI is uncertain
    if escalate:
        compliance_email = settings.get("compliance_email") or settings.get("ops_mailbox")
        if compliance_email:
            from core.graph_client import send_email
            flags_text = "\n".join(f"  \u2022 {f}" for f in review.get("flags", [])) or "  \u2022 Low AI confidence — manual review required"
            send_email(
                settings=settings,
                to=compliance_email,
                cc=[],
                subject=f"KYC Escalation: Manual Review Required \u2014 {investor['entity_name']}",
                body=(
                    f"The KYC AI was unable to make a confident determination for the following document upload.\n\n"
                    f"Investor: {investor['entity_name']}\n"
                    f"File: {filename}{(' (from zip: ' + source_archive + ')') if source_archive else ''}\n"
                    f"AI Confidence: {review.get('confidence', 'unknown')}\n"
                    f"Document Type Detected: {review.get('document_type_detected', 'Unknown')}\n\n"
                    f"Flags:\n{flags_text}\n\n"
                    f"Please review the document manually in the investor's SharePoint folder."
                ),
            )
        logger.warning("KYC parser escalated %s to compliance (%s).", investor["entity_name"], compliance_email)

    # Handle nested entities that require KYC
    nested_entities = review.get("nested_entities", [])
    nested_results = []

    for entity in nested_entities:
        if not entity.get("requires_kyc"):
            continue

        entity_name = entity["name"]
        entity_type = entity.get("type", "Individual")

        # Upsert stub investor for the nested entity
        stub_resp = (
            supabase.table("investors")
            .upsert(
                {
                    "firm_id": firm_id,
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "advisor_email": investor.get("advisor_email"),
                    "kyc_status": "Pending",
                },
                on_conflict="entity_name",
            )
            .execute()
        )
        nested_investor = stub_resp.data[0]
        nested_investor_id = nested_investor["id"]

        # Provision SharePoint folder for nested entity
        # Use the same fund name context from the parent investor's first active commitment
        commitment = (
            supabase.table("commitments")
            .select("deal_id")
            .eq("investor_id", investor_id)
            .eq("status", "Active")
            .limit(1)
            .execute()
        )
        fund_name = "KYC"
        if commitment.data:
            deal = (
                supabase.table("deals")
                .select("offering_name")
                .eq("id", commitment.data[0]["deal_id"])
                .single()
                .execute()
                .data
            )
            fund_name = deal["offering_name"] if deal else "KYC"

        folder_result = create_kyc_folder(
            settings=settings,
            entity_name=entity_name,
            fund_name=fund_name,
        )
        supabase.table("investors").update({
            "sharepoint_folder_id": folder_result["folder_id"],
            "sharepoint_link": folder_result["sharepoint_link"],
        }).eq("id", nested_investor_id).execute()

        # Send KYC request email for nested entity
        kyc_email = build_kyc_email(
            entity_name=entity_name,
            entity_type=entity_type,
            offering_name=fund_name,
            sharepoint_upload_link=folder_result["sharepoint_link"],
            ops_contact_email=settings.get("ops_mailbox"),
        )

        # Send to parent investor's advisor and ops if no direct email for nested entity
        recipient = investor.get("advisor_email") or settings.get("ops_mailbox")
        if recipient:
            send_email(
                settings=settings,
                to=recipient,
                cc=[settings.get("ops_mailbox")] if settings.get("ops_mailbox") != recipient else [],
                subject=kyc_email["subject"],
                body=kyc_email["body"],
            )

        logger.info("Nested entity stub created and KYC request sent: %s", entity_name)
        nested_results.append({"entity_name": entity_name, "investor_id": nested_investor_id})

    # --- Phase 3: Write extracted fields to investor record ---
    if not escalate:
        confidence = review.get("confidence", "low")
        if confidence == "high":
            _write_extracted_fields(investor_id, review)
        elif confidence == "medium":
            _queue_extracted_fields_for_review(investor_id, firm_id, review, source_doc_url)

    # --- Phase 3: Trigger sub doc send when KYC is approved ---
    if new_kyc_status == "Approved" and not escalate:
        _trigger_subdoc_if_ready(investor_id, firm_id, investor, settings)

    return {
        "investor_id": investor_id,
        "kyc_status": new_kyc_status,
        "flags": review.get("flags", []),
        "nested_entities_processed": nested_results,
    }


# ---------------------------------------------------------------------------
# Phase 3 helpers: field extraction and sub doc trigger
# ---------------------------------------------------------------------------

def _write_extracted_fields(investor_id: str, review: dict) -> None:
    """Write high-confidence extracted fields directly to the investors record."""
    updates: dict = {}

    if review.get("state_of_formation"):
        updates["state_of_formation"] = review["state_of_formation"]
    if review.get("formation_date"):
        updates["formation_date"] = review["formation_date"]

    if updates:
        try:
            supabase.table("investors").update(updates).eq("id", investor_id).execute()
            logger.info("Wrote extracted fields to investor %s: %s", investor_id, list(updates.keys()))
        except Exception as e:
            logger.error("Failed to write extracted KYC fields: %s", e)


def _queue_extracted_fields_for_review(
    investor_id: str, firm_id: str, review: dict, source_doc_url: Optional[str]
) -> None:
    """
    Route medium-confidence extracted fields to investor_pending_changes
    so ops can confirm before they're used to pre-fill sub docs.
    """
    fields_to_review: dict[str, str] = {}
    if review.get("state_of_formation"):
        fields_to_review["state_of_formation"] = review["state_of_formation"]
    if review.get("formation_date"):
        fields_to_review["formation_date"] = str(review["formation_date"])

    for field_name, proposed_value in fields_to_review.items():
        try:
            supabase.table("investor_pending_changes").insert({
                "firm_id": firm_id,
                "investor_id": investor_id,
                "field_name": field_name,
                "proposed_value": proposed_value,
                "source": "kyc_extraction",
                "source_doc_url": source_doc_url,
                "status": "Pending",
            }).execute()
        except Exception as e:
            logger.error("Failed to queue KYC field %s for review: %s", field_name, e)


def _trigger_subdoc_if_ready(
    investor_id: str, firm_id: str, investor: dict, settings: dict
) -> None:
    """
    After KYC is approved, check whether sub docs should be sent automatically
    or held pending ops review of low-confidence pre-fill extractions.
    """
    from core.database import supabase as _db

    # Find active commitment that hasn't had sub docs sent yet
    result = (
        _db.table("commitments")
        .select("id, investor_id, deal_id, committed_amount, advisory_fee_pct")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .is_("envelope_id", "null")
        .limit(1)
        .execute()
    )
    if not result.data:
        return  # Sub docs already sent or no active commitment

    commitment = result.data[0]

    # Check for pending pre-fill reviews from KYC extraction
    pending = (
        _db.table("investor_pending_changes")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("source", "kyc_extraction")
        .eq("status", "Pending")
        .limit(1)
        .execute()
    )

    if pending.data:
        # Notify ops to review pre-fill data before sub docs go out
        _notify_ops_prefill_review(investor_id, commitment["id"], firm_id, investor, settings)
        return

    # All clear — send sub docs automatically
    investor_record = (
        _db.table("investors").select("*").eq("id", investor_id).single().execute().data
    )
    deal_record = (
        _db.table("deals").select("*").eq("id", commitment["deal_id"]).single().execute().data
    )
    if not investor_record or not deal_record:
        return

    try:
        from core.docusign_client import send_envelope
        envelope_result = send_envelope(
            settings=settings,
            investor=investor_record,
            deal=deal_record,
            commitment=commitment,
        )
        _db.table("commitments").update({
            "envelope_id": envelope_result["envelope_id"],
            "docusign_status": "Sent",
        }).eq("id", commitment["id"]).execute()

        # Send Email 1 — DocuSign dispatch notice
        if investor_record.get("primary_email"):
            from core.email_templates import build_docusign_dispatch_email
            from core.graph_client import send_email
            email1 = build_docusign_dispatch_email(
                entity_name=investor_record["entity_name"],
                offering_name=deal_record["offering_name"],
                committed_amount=float(commitment.get("committed_amount") or 0),
                advisor_name=investor_record.get("advisor_email"),
                ops_contact_email=settings.get("ops_mailbox"),
                firm_id=firm_id,
            )
            send_email(
                settings=settings,
                to=investor_record["primary_email"],
                cc=[investor_record["advisor_email"]] if investor_record.get("advisor_email") else [],
                subject=email1["subject"],
                body=email1["body"],
            )

        logger.info("Sub docs auto-sent for %s after KYC approval.", investor_record["entity_name"])
    except Exception as e:
        logger.error("Failed to auto-send sub docs for investor %s: %s", investor_id, e)


def _notify_ops_prefill_review(
    investor_id: str, commitment_id: str, firm_id: str, investor: dict, settings: dict
) -> None:
    """Send ops a notification that pre-fill data needs review before sub docs are sent."""
    ops_email = settings.get("ops_mailbox")
    if not ops_email:
        return
    try:
        from core.graph_client import send_email
        base_url = settings.get("platform_base_url", "https://example.com")
        review_url = f"{base_url}/commitments/{commitment_id}/prefill-preview"
        send_email(
            settings=settings,
            to=ops_email,
            cc=[],
            subject=f"Pre-fill Review Required Before Sub Docs — {investor.get('entity_name', '')}",
            body=(
                f"KYC documents for {investor.get('entity_name', 'this investor')} have been approved, "
                f"but some extracted fields need your confirmation before sub docs can be sent.\n\n"
                f"Please review and confirm the pre-fill data:\n{review_url}\n\n"
                f"Once confirmed, sub docs will be sent automatically."
            ),
        )
    except Exception as e:
        logger.error("Failed to send pre-fill review notification: %s", e)
