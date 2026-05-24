"""
LOI (Letter of Intent) routes.

POST /loi/send/{commitment_id}  — ops triggers LOI DocuSign send for a commitment
GET  /loi/status/{commitment_id} — check LOI status for a commitment
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from core.database import supabase

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _get_commitment_with_relations(commitment_id: str, firm_id: str) -> dict:
    result = (
        supabase.table("commitments")
        .select("id, firm_id, committed_amount, loi_status, loi_envelope_id, investors(id, entity_name, primary_email, advisor_email, sharepoint_folder_id), deals(id, offering_name)")
        .eq("id", commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Commitment not found.")
    return result.data


def _get_firm_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


@router.post("/send/{commitment_id}")
def send_loi(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Send an LOI DocuSign envelope for a specific commitment.
    Only available to ops and admin roles.
    If an LOI has already been sent for this commitment, raises a 409 conflict.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment_with_relations(commitment_id, firm_id)

    if commitment.get("loi_status") not in ("Not Sent", None):
        raise HTTPException(
            status_code=409,
            detail=f"LOI already sent for this commitment (status: {commitment['loi_status']}). Use /loi/status/{commitment_id} to check.",
        )

    settings = _get_firm_settings(firm_id)

    if not settings.get("docusign_loi_template_id"):
        raise HTTPException(
            status_code=422,
            detail="docusign_loi_template_id is not configured in firm settings. Add it before sending LOIs.",
        )

    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})

    if not investor.get("primary_email"):
        raise HTTPException(
            status_code=422,
            detail=f"Investor {investor.get('entity_name')} has no primary_email on record. Add it before sending an LOI.",
        )

    from core.docusign_client import send_loi_envelope

    result = send_loi_envelope(
        settings=settings,
        investor=investor,
        deal=deal,
        commitment=commitment,
    )
    loi_envelope_id = result["envelope_id"]

    # Update commitment with LOI envelope ID and status
    supabase.table("commitments").update({
        "loi_envelope_id": loi_envelope_id,
        "loi_status": "Sent",
    }).eq("id", commitment_id).execute()

    logger.info(
        "LOI envelope sent to %s (%s). envelope_id=%s",
        investor["entity_name"],
        investor["primary_email"],
        loi_envelope_id,
    )

    return {
        "status": "sent",
        "commitment_id": commitment_id,
        "loi_envelope_id": loi_envelope_id,
        "investor": investor.get("entity_name"),
        "fund": deal.get("offering_name"),
    }


@router.get("/status/{commitment_id}")
def get_loi_status(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Return the current LOI status for a commitment."""
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment_with_relations(commitment_id, firm_id)

    return {
        "commitment_id": commitment_id,
        "investor": commitment.get("investors", {}).get("entity_name"),
        "fund": commitment.get("deals", {}).get("offering_name"),
        "loi_status": commitment.get("loi_status", "Not Sent"),
        "loi_envelope_id": commitment.get("loi_envelope_id"),
    }
