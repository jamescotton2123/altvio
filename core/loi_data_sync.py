"""
LOI Data Sync — Master Database Auto-Update.
When an LOI is signed, GPT-4o Vision scans the PDF for updated investor
contact information and syncs it back to the investors record.

Auto-updated fields (no human approval required):
  - primary_email
  - mailing_address

Flagged for ops review (require human sign-off before applying):
  - entity_name  (legal name changes have compliance implications)
  - tax_id       (SSN/EIN changes require manual verification)

The audit_log trigger on the investors table captures every field change
automatically for compliance recordkeeping.
"""

import base64
import json
import logging

from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """
You are reviewing a signed Letter of Intent (LOI) document for an alternative investment firm.
Extract the following investor contact information fields from the document.

Return ONLY a valid JSON object with these exact keys:
{
  "entity_name": "Full legal entity name as it appears on the LOI",
  "primary_email": "Email address",
  "mailing_address": "Full mailing address",
  "tax_id": "EIN or SSN (masked if partially visible, e.g. XX-XXX1234)"
}

If a field is not present or not legible in the document, set it to null.
Do not infer or guess — only extract what is explicitly on the document.
"""

AUTO_UPDATE_FIELDS = {"primary_email", "mailing_address"}
FLAG_FIELDS = {"entity_name", "tax_id"}


def _extract_loi_fields(pdf_bytes: bytes) -> dict:
    """Use GPT-4o Vision to extract contact fields from a signed LOI PDF."""
    encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACT_PROMPT},
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
    return json.loads(response.choices[0].message.content)


def sync_investor_from_loi(
    investor_id: str,
    firm_id: str,
    pdf_bytes: bytes,
    settings: dict,
) -> dict:
    """
    Main entry point. Called after a signed LOI PDF is saved to SharePoint.

    1. Extract contact fields from the PDF using GPT-4o Vision.
    2. Compare against the current investors record.
    3. Auto-apply changes to primary_email and mailing_address.
    4. Flag entity_name and tax_id changes for ops review.
    5. Return a summary of what was updated and what was flagged.
    """
    from core.database import supabase

    # Fetch current investor record
    investor = (
        supabase.table("investors")
        .select("entity_name, primary_email, mailing_address, tax_id")
        .eq("id", investor_id)
        .single()
        .execute()
        .data
    )
    if not investor:
        raise ValueError(f"Investor {investor_id} not found.")

    extracted = _extract_loi_fields(pdf_bytes)

    auto_updates = {}
    flagged = []

    for field, extracted_value in extracted.items():
        if not extracted_value:
            continue

        current_value = investor.get(field)

        if extracted_value == current_value:
            continue

        if field in AUTO_UPDATE_FIELDS:
            auto_updates[field] = extracted_value

        elif field in FLAG_FIELDS:
            supabase.table("investor_pending_changes").insert({
                "firm_id": firm_id,
                "investor_id": investor_id,
                "field_name": field,
                "current_value": current_value,
                "proposed_value": extracted_value,
                "source": "loi_sync",
                "status": "Pending",
            }).execute()
            flagged.append({
                "field": field,
                "current": current_value,
                "proposed": extracted_value,
            })

    # Apply auto-updates in a single DB call
    if auto_updates:
        supabase.table("investors").update(auto_updates).eq("id", investor_id).execute()
        logger.info("LOI sync auto-updated investor %s: %s", investor_id, list(auto_updates.keys()))

    if flagged:
        logger.warning("LOI sync flagged investor %s for ops review: %s", investor_id, [f["field"] for f in flagged])

    return {
        "investor_id": investor_id,
        "updated_fields": list(auto_updates.keys()),
        "flagged_fields": flagged,
    }


def apply_pending_change(
    change_id: str,
    investor_id: str,
    firm_id: str,
    approved: bool,
    reviewed_by: str,
) -> dict:
    """
    Ops approves or rejects a flagged investor field change.
    If approved, applies the change to the investors record.
    Either way, marks the pending_change as resolved.
    """
    from datetime import datetime, timezone

    from core.database import supabase

    change = (
        supabase.table("investor_pending_changes")
        .select("*")
        .eq("id", change_id)
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not change:
        raise ValueError(f"Pending change {change_id} not found.")

    new_status = "Approved" if approved else "Rejected"

    supabase.table("investor_pending_changes").update({
        "status": new_status,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", change_id).execute()

    if approved:
        supabase.table("investors").update({
            change["field_name"]: change["proposed_value"],
        }).eq("id", investor_id).execute()
        logger.info("LOI sync applied change: investors.%s updated for %s", change["field_name"], investor_id)

    return {
        "change_id": change_id,
        "field": change["field_name"],
        "status": new_status,
        "applied": approved,
    }
