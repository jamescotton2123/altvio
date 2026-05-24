"""
DocuSign webhook routes.
POST /docusign/webhook — handles DocuSign Connect event notifications.

Events handled:
  1. recipient-completed (routing order 3) → sub doc AI review (sub doc envelopes only)
  2. envelope-completed (TOI) → signed TOI PDF to transferor SharePoint, transfer status Complete
  3. envelope-completed (LOI) → signed LOI PDF, loi_status, optional LOI data sync
  4. envelope-completed (subscription) → download PDFs, Email 2, commitment_date
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from core.database import supabase

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_firm_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


def _lookup_webhook_firm_id(body: dict, envelope_id: str) -> str:
    """Resolve firm_id with read-only lookups needed before HMAC verification."""
    firm_id = (
        body.get("firm_id")
        or body.get("firmId")
        or body.get("data", {}).get("firm_id")
        or body.get("data", {}).get("firmId")
    )
    if firm_id:
        return firm_id

    for column in ("envelope_id", "loi_envelope_id"):
        result = (
            supabase.table("commitments")
            .select("firm_id")
            .eq(column, envelope_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["firm_id"]

    result = (
        supabase.table("transfers_of_interest")
        .select("firm_id")
        .eq("toi_envelope_id", envelope_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["firm_id"]

    raise HTTPException(status_code=404, detail=f"No firm found for envelope_id={envelope_id}")


def _verify_docusign_hmac(raw_body: bytes, signature_header: str | None, settings: dict) -> None:
    secret = settings.get("docusign_connect_hmac_secret")
    if not signature_header or not secret:
        logger.warning("DocuSign webhook rejected: missing HMAC signature or secret.")
        raise HTTPException(status_code=401, detail="Invalid DocuSign signature.")

    try:
        provided_digest = base64.b64decode(signature_header, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("DocuSign webhook rejected: malformed HMAC signature.")
        raise HTTPException(status_code=401, detail="Invalid DocuSign signature.")

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(provided_digest, expected_digest):
        logger.warning("DocuSign webhook rejected: HMAC signature mismatch.")
        raise HTTPException(status_code=401, detail="Invalid DocuSign signature.")


def _lookup_commitment(envelope_id: str) -> dict:
    """Look up commitment by sub doc envelope_id."""
    result = (
        supabase.table("commitments")
        .select("id, firm_id, investor_id, deal_id, committed_amount, docusign_status, investors(entity_name, primary_email, advisor_email, sharepoint_folder_id), deals(offering_name, wire_instructions, wire_instructions_legacy)")
        .eq("envelope_id", envelope_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"No commitment found for envelope_id={envelope_id}")
    return result.data


def _lookup_loi_commitment(loi_envelope_id: str) -> dict | None:
    """Look up commitment by loi_envelope_id. Returns None if not found (not an LOI envelope)."""
    result = (
        supabase.table("commitments")
        .select("id, firm_id, investor_id, deal_id, investors(entity_name, primary_email, advisor_email, sharepoint_folder_id), deals(offering_name)")
        .eq("loi_envelope_id", loi_envelope_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]


def _lookup_toi_transfer(envelope_id: str) -> dict | None:
    """Match a completed DocuSign envelope to a Transfer of Interest row."""
    result = (
        supabase.table("transfers_of_interest")
        .select(
            "id, firm_id, commitment_id, transfer_amount, toi_envelope_id, "
            "transferor:transferor_investor_id(entity_name, primary_email, advisor_email, sharepoint_folder_id), "
            "transferee:transferee_investor_id(entity_name, primary_email, sharepoint_folder_id), "
            "commitments(deal_id, deals(offering_name))"
        )
        .eq("toi_envelope_id", envelope_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]


@router.post("/webhook")
async def docusign_webhook(request: Request):
    """
    Receive DocuSign Connect webhook events.
    Handles both partial-signing (recipient-completed) and full completion.
    """
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event = body.get("event")
    envelope_id = body.get("data", {}).get("envelopeId") or body.get("envelopeId")

    if not envelope_id:
        return {"status": "ignored", "reason": "No envelopeId in payload"}

    firm_id = _lookup_webhook_firm_id(body, envelope_id)
    settings = _get_firm_settings(firm_id)
    _verify_docusign_hmac(
        raw_body,
        request.headers.get("X-DocuSign-Signature-1"),
        settings,
    )
    insert_result = supabase.table("webhook_events").upsert({
        "source": "docusign",
        "external_id": f"{envelope_id}:{event}",
        "firm_id": firm_id,
        "payload": body,
    }, on_conflict="source,external_id", ignore_duplicates=True).execute()
    if not insert_result.data:
        return {"status": "already_processed"}

    if event == "recipient-completed":
        return await _handle_recipient_completed(body, envelope_id)

    if event == "envelope-completed":
        toi_transfer = _lookup_toi_transfer(envelope_id)
        if toi_transfer:
            return await _handle_toi_completed(body, envelope_id, toi_transfer)
        loi_commitment = _lookup_loi_commitment(envelope_id)
        if loi_commitment:
            return await _handle_loi_completed(body, envelope_id, loi_commitment)
        return await _handle_envelope_completed(body, envelope_id)

    return {"status": "ignored", "event": event}


async def _handle_recipient_completed(body: dict, envelope_id: str) -> dict:
    """
    Routing order 3 (investor) has signed.
    Trigger the sub doc AI review before Advisor (order 4) proceeds.
    Chain: Reviewer(1) → Ops(2) → Investor(3) → Advisor(4) → Compliance(5) → CEO(6).
    """
    if _lookup_toi_transfer(envelope_id):
        return {"status": "ignored", "reason": "toi_envelope"}

    from core.subdoc_reviewer import process_subdoc_review

    recipient_data = body.get("data", {}).get("recipient", {})
    routing_order = str(recipient_data.get("routingOrder", ""))

    if routing_order != "3":
        return {"status": "ignored", "reason": f"routing_order={routing_order}, not investor"}

    commitment = _lookup_commitment(envelope_id)
    commitment_id = commitment["id"]
    firm_id = commitment["firm_id"]
    settings = _get_firm_settings(firm_id)

    review = process_subdoc_review(
        envelope_id=envelope_id,
        commitment_id=commitment_id,
        firm_id=firm_id,
        settings=settings,
    )

    return {
        "status": "subdoc_review_complete",
        "approved": review["approved"],
        "flags": review["flags"],
    }


async def _handle_envelope_completed(body: dict, envelope_id: str) -> dict:
    """
    All signers (investor + ops) have completed.
    1. Download signed PDFs from DocuSign.
    2. Save to investor's SharePoint folder.
    3. Update commitment docusign_status = 'Signed'.
    4. Send Email 2 (signed docs + wire instructions).
    """
    from core.audit import log_audit
    from core.docusign_client import download_signed_documents, truncate_entity_name
    from core.email_templates import build_signed_docs_wire_email
    from core.graph_client import (
        build_sp_document_filename,
        save_document_to_folder,
        send_email,
    )

    commitment = _lookup_commitment(envelope_id)
    commitment_id = commitment["id"]
    firm_id = commitment["firm_id"]
    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})
    settings = _get_firm_settings(firm_id)

    # Download signed PDFs
    pdf_bytes = download_signed_documents(envelope_id, settings)

    # Save to SharePoint investor folder — filename from firm_settings.file_naming_template
    folder_id = investor.get("sharepoint_folder_id")
    if folder_id and pdf_bytes:
        entity_display = truncate_entity_name(investor.get("entity_name", "Investor"), max_len=35)
        fund_display = deal.get("offering_name", "Fund") or "Fund"
        filename = build_sp_document_filename(
            settings.get("file_naming_template"),
            entity_display,
            fund_display,
            "SignedDocs.pdf",
        )
        save_document_to_folder(settings, folder_id, filename, pdf_bytes)

    # Update commitment status and record commitment_date (CEO was final signer)
    completion_ts = (
        body.get("data", {}).get("envelopeSummary", {}).get("completedDateTime")
        or datetime.now(timezone.utc).isoformat()
    )
    supabase.table("commitments").update({
        "docusign_status": "Signed",
        "commitment_date": completion_ts,
    }).eq("id", commitment_id).execute()

    # Rebuild SharePoint link for email
    investor_record = (
        supabase.table("investors")
        .select("sharepoint_link")
        .eq("id", commitment["investor_id"])
        .single()
        .execute()
        .data
    )
    sharepoint_link = investor_record.get("sharepoint_link", "") if investor_record else ""

    # Format wire instructions from structured JSONB (or legacy text fallback)
    from api.routes.deal_hub import _format_wire_instructions
    wire_delivery_mode = settings.get("wire_delivery_mode", "inline")
    formatted_wire = _format_wire_instructions(deal.get("wire_instructions"))

    # Generate portal access token if wire_delivery_mode = 'portal'
    portal_url = None
    if wire_delivery_mode == "portal":
        try:
            from core.portal import generate_portal_token
            expiry_days = settings.get("portal_link_expiry_days") or 30
            portal_result = generate_portal_token(
                firm_id=firm_id,
                investor_id=commitment["investor_id"],
                commitment_id=commitment_id,
                expiry_days=expiry_days,
            )
            portal_url = portal_result["portal_url"]
        except Exception as e:
            logger.warning("DocuSign portal token generation failed: %s", e)

    from core.deal_fees import compute_commitment_wire_breakdown
    wire_breakdown = compute_commitment_wire_breakdown(
        committed_amount=float(commitment.get("committed_amount") or 0),
        deal_id=commitment["deal_id"],
        firm_id=firm_id,
    )

    # Send Email 2 — wire/docs via inline, secure folder link, or branded investor portal
    email2 = build_signed_docs_wire_email(
        entity_name=investor.get("entity_name", ""),
        offering_name=deal.get("offering_name", ""),
        committed_amount=commitment["committed_amount"],
        sharepoint_link=sharepoint_link,
        wire_instructions=formatted_wire,
        advisor_name=investor.get("advisor_email"),
        ops_contact_email=settings.get("ops_mailbox"),
        wire_delivery_mode=wire_delivery_mode,
        portal_url=portal_url,
        wire_breakdown=wire_breakdown,
        firm_id=firm_id,
    )

    send_email(
        settings=settings,
        to=investor["primary_email"],
        cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
        subject=email2["subject"],
        body=email2["body"],
    )

    if settings.get("wire_extraction_enabled", False):
        try:
            from core.wire_extractor import extract_wire_from_pdf

            extract_wire_from_pdf(
                commitment["investor_id"],
                commitment_id,
                firm_id,
                pdf_bytes,
                settings,
            )
        except Exception as e:
            logger.warning("Wire extractor failed during DocuSign processing: %s", e)

    log_audit(
        firm_id=firm_id,
        actor_type="system",
        actor_id=None,
        action="docusign.envelope_completed",
        entity_type="commitment",
        entity_id=commitment_id,
        before={"docusign_status": commitment.get("docusign_status")},
        after={"docusign_status": "Signed", "commitment_date": completion_ts},
        metadata={"source": "docusign_webhook", "envelope_id": envelope_id},
    )
    logger.info(
        "DocuSign envelope %s complete. Wire email sent to %s.",
        envelope_id,
        investor.get("primary_email"),
    )
    return {"status": "envelope_complete", "commitment_id": commitment_id}


async def _handle_loi_completed(body: dict, envelope_id: str, commitment: dict) -> dict:
    """
    LOI envelope has been fully signed by the investor.
    1. Download signed LOI PDF from DocuSign.
    2. Save to investor's SharePoint folder as {entity_name}_{fund}_LOI_Signed.pdf.
    3. Update commitments.loi_status = 'Signed'.
    """
    from core.docusign_client import download_signed_documents, truncate_entity_name
    from core.graph_client import build_sp_document_filename, save_document_to_folder

    commitment_id = commitment["id"]
    firm_id = commitment["firm_id"]
    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})
    settings = _get_firm_settings(firm_id)

    # Download signed LOI PDF
    pdf_bytes = download_signed_documents(envelope_id, settings)

    # Save to investor's SharePoint folder — filename from firm_settings.file_naming_template
    folder_id = investor.get("sharepoint_folder_id")
    if folder_id and pdf_bytes:
        entity_display = truncate_entity_name(investor.get("entity_name", "Investor"), max_len=35)
        fund_display = deal.get("offering_name", "Fund") or "Fund"
        filename = build_sp_document_filename(
            settings.get("file_naming_template"),
            entity_display,
            fund_display,
            "LOI_Signed.pdf",
        )
        save_document_to_folder(settings, folder_id, filename, pdf_bytes)
        logger.info("DocuSign LOI saved to SharePoint: %s", filename)

    # Update LOI status on commitment
    supabase.table("commitments").update({
        "loi_status": "Signed",
    }).eq("id", commitment_id).execute()

    # Sync any updated contact fields from the signed LOI back to the master investor record
    if pdf_bytes:
        try:
            from core.loi_data_sync import sync_investor_from_loi
            sync_result = sync_investor_from_loi(
                investor_id=commitment["investor_id"],
                firm_id=firm_id,
                pdf_bytes=pdf_bytes,
                settings=settings,
            )
            logger.info(
                "DocuSign LOI sync complete. updated=%s flagged=%s",
                sync_result["updated_fields"],
                [f["field"] for f in sync_result["flagged_fields"]],
            )
        except Exception as e:
            logger.warning("DocuSign LOI data sync failed: %s", e)

    logger.info(
        "DocuSign LOI envelope %s complete for %s.",
        envelope_id,
        investor.get("entity_name"),
    )
    return {"status": "loi_complete", "commitment_id": commitment_id}


async def _handle_toi_completed(body: dict, envelope_id: str, transfer: dict) -> dict:
    """
    TOI envelope fully signed — archive PDF to transferor SharePoint folder and close transfer row.
    Commitment economics (split / new commitment for transferee) remain an ops workflow outside this hook.
    """
    from core.docusign_client import download_signed_documents, truncate_entity_name
    from core.graph_client import build_sp_document_filename, save_document_to_folder

    transfer_id = transfer["id"]
    firm_id = transfer["firm_id"]
    transferor = transfer.get("transferor") or {}
    comm = transfer.get("commitments") or {}
    if isinstance(comm, list):
        comm = comm[0] if comm else {}
    deal = comm.get("deals") or {}
    if isinstance(deal, list):
        deal = deal[0] if deal else {}

    settings = _get_firm_settings(firm_id)
    pdf_bytes = download_signed_documents(envelope_id, settings)

    folder_id = transferor.get("sharepoint_folder_id")
    if folder_id and pdf_bytes:
        entity_display = truncate_entity_name(transferor.get("entity_name", "Transferor"), max_len=35)
        fund_display = deal.get("offering_name") or "Fund"
        filename = build_sp_document_filename(
            settings.get("file_naming_template"),
            entity_display,
            fund_display,
            "TOI_Signed.pdf",
        )
        save_document_to_folder(settings, folder_id, filename, pdf_bytes)
        logger.info("DocuSign TOI saved to SharePoint: %s", filename)

    completion_ts = (
        body.get("data", {}).get("envelopeSummary", {}).get("completedDateTime")
        or datetime.now(timezone.utc).isoformat()
    )
    supabase.table("transfers_of_interest").update({
        "status": "Complete",
        "transfer_date": completion_ts[:10] if completion_ts else None,
    }).eq("id", transfer_id).execute()

    logger.info("DocuSign TOI envelope %s complete for transfer %s.", envelope_id, transfer_id)
    return {"status": "toi_complete", "transfer_id": transfer_id}
