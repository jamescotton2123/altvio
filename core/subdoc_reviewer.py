"""
Agentic sub doc reviewer.
Triggered by the DocuSign recipient-completed webhook (routing order 1 = investor signs).
Downloads the partially-signed envelope, sends to GPT-4o Vision, scans for:
  - Accredited investor status not confirmed
  - ERISA plan percentage over threshold
  - Bad actor representations checked incorrectly
  - Commitment amount mismatch vs DB record
  - Extracts key investor data fields to populate the active deal tracker

If clean → triggers ops countersign (routing order 2) via DocuSign API.
If flagged → pauses envelope, notifies ops.
"""

import base64
import json
import logging
import os

from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

logger = logging.getLogger(__name__)

REVIEW_PROMPT = """
You are a compliance officer reviewing a signed alternative investment subscription document.
You have been provided with a page from a signed subscription agreement as an image.

Carefully scan the document and return a JSON object with the following structure:

{
  "approved": true or false,
  "flags": [
    // List of specific issues found. Empty array if clean.
    // Examples: "Investor did not confirm accredited investor status",
    //           "ERISA plan assets percentage exceeds 25% threshold",
    //           "Bad actor representation answered incorrectly",
    //           "Commitment amount on document does not match expected amount"
  ],
  "extracted_data": {
    "entity_name": "...",
    "entity_type": "...",
    "tax_id": "...",
    "mailing_address": "...",
    "investment_amount": 0,
    "date_signed": "...",
    "signatory_name": "...",
    "signatory_title": "..."
  },
  "accredited_investor_confirmed": true or false or null,
  "erisa_percentage": null or number,
  "bad_actor_clean": true or false or null,
  "notes": "Any additional observations"
}

Be precise. If a field is not visible or determinable from this page, set it to null.
Approve ONLY if all compliance representations are properly checked and no anomalies are found.
"""


def review_signed_document(
    pdf_bytes: bytes,
    expected_amount: float,
    settings: dict,
) -> dict:
    """
    Review a signed subscription document for compliance issues.

    Args:
        pdf_bytes: Raw bytes of the signed PDF from DocuSign.
        expected_amount: The committed_amount from the DB to cross-check.
        settings: Firm settings dict.

    Returns:
        {
            "approved": bool,
            "flags": list[str],
            "extracted_data": dict,
            "raw_response": dict
        }
    """
    # Encode PDF as base64 for GPT-4o Vision
    encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": REVIEW_PROMPT
                        + f"\n\nExpected commitment amount from our records: ${expected_amount:,.2f}. "
                        "Flag if the document shows a different amount.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:application/pdf;base64,{encoded}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        temperature=0,
    )

    result = json.loads(response.choices[0].message.content)

    return {
        "approved": result.get("approved", False),
        "flags": result.get("flags", []),
        "extracted_data": result.get("extracted_data", {}),
        "raw_response": result,
    }


def _queue_subdoc_fields_for_review(
    investor_id: str,
    firm_id: str,
    extracted: dict,
    envelope_id: str,
) -> None:
    """Queue sub-doc extracted fields for ops review (source=subdoc_extraction)."""
    from core.database import supabase as _db

    fields_to_queue = {
        "signatory_name": extracted.get("signatory_name"),
        "signatory_title": extracted.get("signatory_title"),
        "investment_amount": extracted.get("investment_amount"),
        "date_signed": extracted.get("date_signed"),
    }
    source_doc_url = f"docusign://envelope/{envelope_id}"

    for field_name, proposed_value in fields_to_queue.items():
        if proposed_value is None or proposed_value == "":
            continue
        try:
            _db.table("investor_pending_changes").insert({
                "firm_id": firm_id,
                "investor_id": investor_id,
                "field_name": field_name,
                "proposed_value": proposed_value,
                "source": "subdoc_extraction",
                "source_doc_url": source_doc_url,
                "status": "Pending",
            }).execute()
        except Exception as exc:
            logger.error(
                "Failed to queue subdoc field %s for investor %s: %s",
                field_name,
                investor_id,
                exc,
            )


def process_subdoc_review(
    envelope_id: str,
    commitment_id: str,
    firm_id: str,
    settings: dict,
) -> dict:
    """
    Full sub doc review flow. Called by the DocuSign webhook handler
    when routing order 1 (investor) completes.

    1. Download partially-signed PDF from DocuSign.
    2. Run AI compliance review.
    3. If approved → update DB + notify ops to countersign.
    4. If flagged → pause envelope + alert ops.
    """
    from core.database import supabase
    from core.docusign_client import download_signed_documents, pause_envelope
    from core.graph_client import send_email

    # Fetch commitment to get expected amount
    commitment = (
        supabase.table("commitments")
        .select("committed_amount, investor_id, deal_id")
        .eq("id", commitment_id)
        .single()
        .execute()
        .data
    )

    # Download the partially-signed document
    pdf_bytes = download_signed_documents(envelope_id, settings)

    # Run the AI review
    review = review_signed_document(
        pdf_bytes=pdf_bytes,
        expected_amount=commitment["committed_amount"],
        settings=settings,
    )

    investor_id = commitment["investor_id"]

    # Update deal tracker with extracted investor data
    if review["extracted_data"]:
        extracted = review["extracted_data"]
        high_confidence = {k: v for k, v in {
            "entity_type": extracted.get("entity_type"),
            "mailing_address": extracted.get("mailing_address"),
            "tax_id": extracted.get("tax_id"),
        }.items() if v is not None}
        if high_confidence:
            supabase.table("investors").update(high_confidence).eq(
                "id", investor_id
            ).execute()

        _queue_subdoc_fields_for_review(
            investor_id=investor_id,
            firm_id=firm_id,
            extracted=extracted,
            envelope_id=envelope_id,
        )

    if review["approved"]:
        # Update commitment status — ops countersign is now live in DocuSign routing
        supabase.table("commitments").update({
            "docusign_status": "Pending Countersign",
        }).eq("id", commitment_id).execute()
        logger.info("Sub doc review approved. envelope_id=%s routed to ops countersign.", envelope_id)
    else:
        # Pause envelope and alert ops
        flags_summary = "\n".join(f"  • {f}" for f in review["flags"])
        pause_envelope(envelope_id, flags_summary, settings)

        supabase.table("commitments").update({
            "docusign_status": "Review Required",
        }).eq("id", commitment_id).execute()

        ops_email = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
        if ops_email:
            send_email(
                settings=settings,
                to=ops_email,
                cc=[],
                subject=f"ACTION REQUIRED: Sub Doc Review Flag — Envelope {envelope_id}",
                body=(
                    f"The following compliance issues were detected in a signed subscription document.\n\n"
                    f"Envelope ID: {envelope_id}\n"
                    f"Commitment ID: {commitment_id}\n\n"
                    f"FLAGS:\n{flags_summary}\n\n"
                    "The envelope has been paused. Please review and take action in DocuSign."
                ),
            )
        logger.warning("Sub doc review flagged. envelope_id=%s flags=%s", envelope_id, review["flags"])

    return review
