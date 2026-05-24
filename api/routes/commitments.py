"""
Commitment management routes.
PATCH /commitments/{id}                   — amount, wire status, liquidation, funding_entity_name (inbound wire FROM)
PATCH /commitments/{id}/fund              — mark inbound subscription wire received; optional funding_entity_name
GET   /commitments/{id}/history           — full audit trail for a commitment
GET   /commitments/{id}/prefill-preview   — ops review of KYC-extracted pre-fill data
POST  /commitments/{id}/confirm-prefill   — confirm/reject extracted fields, then auto-send sub docs
POST  /commitments/{id}/side-letter       — GPT-draft side letter from firm template + provisions
GET   /commitments/{id}/side-letter/preview — retrieve the drafted side letter for ops review
GET   /commitments/{id}/wire-breakdown    — commitment + third-party fee math for investor wire / ops QA
POST  /commitments/{id}/extract-wire      — GPT extraction of distribution payout banking from signed sub doc PDF
"""

import logging
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.deal_fees import compute_commitment_wire_breakdown
from core.funding_source import build_funding_source_fields

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _get_commitment(commitment_id: str, firm_id: str) -> dict:
    result = (
        supabase.table("commitments")
        .select("*, investors(entity_name, advisor_email, primary_email), deals(offering_name)")
        .eq("id", commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Commitment not found.")
    return result.data


def _log_event(firm_id: str, commitment_id: str, event_type: str, old_value: dict, new_value: dict, changed_by: Optional[str] = None):
    supabase.table("commitment_events").insert({
        "firm_id": firm_id,
        "commitment_id": commitment_id,
        "event_type": event_type,
        "old_value": old_value,
        "new_value": new_value,
        "changed_by": changed_by,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


class CommitmentUpdatePayload(BaseModel):
    committed_amount: Optional[float] = None
    status: Optional[str] = None
    wire_status: Optional[str] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    trader_id: Optional[str] = None
    liquidation_required: Optional[bool] = None
    liquidation_due_date: Optional[str] = None
    liquidation_desk_notes: Optional[str] = None
    clear_liquidation_ack: Optional[bool] = None
    funding_entity_name: Optional[str] = None
    funding_entity_kyc_complete: Optional[bool] = None


@router.patch("/{commitment_id}")
def update_commitment(
    commitment_id: str,
    payload: CommitmentUpdatePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Update commitment fields. Amount, status, wire_status, and private-wealth liquidation controls.
    All changes are logged to commitment_events for full audit trail.
    Supported status values: Active, Withdrawn, Modified.

    Liquidation assignments surface in trader / CA digests only when investors.private_wealth is true;
    set investors.client_associate_email so Account Support receives Schwab wire queue emails.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    updates = {}
    old_snapshot = {}
    new_snapshot = {}

    if payload.committed_amount is not None and payload.committed_amount != commitment["committed_amount"]:
        old_snapshot["committed_amount"] = commitment["committed_amount"]
        new_snapshot["committed_amount"] = payload.committed_amount
        updates["committed_amount"] = payload.committed_amount
        updates["status"] = "Modified"

        _log_event(firm_id, commitment_id, "amount_changed", old_snapshot, new_snapshot, payload.changed_by)

    if payload.status == "Withdrawn" and commitment["status"] != "Withdrawn":
        old_snapshot["status"] = commitment["status"]
        new_snapshot["status"] = "Withdrawn"
        updates["status"] = "Withdrawn"

        _log_event(firm_id, commitment_id, "withdrawn", old_snapshot, new_snapshot, payload.changed_by)

        # Notify advisor of the withdrawal
        investor = commitment.get("investors", {})
        deal = commitment.get("deals", {})
        if investor.get("advisor_email"):
            try:
                settings = (
                    supabase.table("firm_settings")
                    .select("*")
                    .eq("firm_id", firm_id)
                    .single()
                    .execute()
                    .data
                )
                from core.graph_client import send_email
                send_email(
                    settings=settings,
                    to=investor["advisor_email"],
                    cc=[settings.get("ops_mailbox")] if settings.get("ops_mailbox") else [],
                    subject=f"Commitment Withdrawn — {investor.get('entity_name')} / {deal.get('offering_name')}",
                    body=(
                        f"This is to confirm that the commitment for {investor.get('entity_name')} "
                        f"in {deal.get('offering_name')} has been marked as Withdrawn.\n\n"
                        f"Reason: {payload.reason or 'Not specified'}\n\n"
                        f"If this is incorrect, please contact operations immediately."
                    ),
                )
            except Exception as e:
                logger.error("Failed to send withdrawal notification: %s", e)

    if payload.wire_status:
        updates["wire_status"] = payload.wire_status
        _log_event(
            firm_id, commitment_id, "wire_status_changed",
            {"wire_status": commitment.get("wire_status")},
            {"wire_status": payload.wire_status},
            payload.changed_by,
        )

    if payload.clear_liquidation_ack:
        updates["liquidation_acknowledged_at"] = None
        _log_event(
            firm_id, commitment_id, "liquidation_ack_cleared",
            {"liquidation_acknowledged_at": commitment.get("liquidation_acknowledged_at")},
            {"liquidation_acknowledged_at": None},
            payload.changed_by,
        )

    if payload.trader_id is not None:
        tid = payload.trader_id.strip() if isinstance(payload.trader_id, str) else payload.trader_id
        if tid == "":
            updates["trader_id"] = None
        else:
            tr = (
                supabase.table("traders")
                .select("id")
                .eq("id", tid)
                .eq("firm_id", firm_id)
                .single()
                .execute()
                .data
            )
            if not tr:
                raise HTTPException(status_code=400, detail="trader_id does not belong to this firm.")
            updates["trader_id"] = tid
        _log_event(
            firm_id,
            commitment_id,
            "trader_assignment_changed",
            {"trader_id": commitment.get("trader_id")},
            {"trader_id": updates.get("trader_id", tid)},
            payload.changed_by,
        )

    if payload.liquidation_required is not None:
        updates["liquidation_required"] = payload.liquidation_required
        _log_event(
            firm_id,
            commitment_id,
            "liquidation_flag_changed",
            {"liquidation_required": commitment.get("liquidation_required")},
            {"liquidation_required": payload.liquidation_required},
            payload.changed_by,
        )

    if payload.liquidation_due_date is not None:
        raw = payload.liquidation_due_date.strip()
        if raw == "":
            updates["liquidation_due_date"] = None
        else:
            try:
                date_type.fromisoformat(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="liquidation_due_date must be YYYY-MM-DD.")
            updates["liquidation_due_date"] = raw
        _log_event(
            firm_id,
            commitment_id,
            "liquidation_due_changed",
            {"liquidation_due_date": commitment.get("liquidation_due_date")},
            {"liquidation_due_date": updates.get("liquidation_due_date")},
            payload.changed_by,
        )

    if payload.liquidation_desk_notes is not None:
        updates["liquidation_desk_notes"] = payload.liquidation_desk_notes.strip() or None
        _log_event(
            firm_id,
            commitment_id,
            "liquidation_notes_changed",
            {"liquidation_desk_notes": commitment.get("liquidation_desk_notes")},
            {"liquidation_desk_notes": updates["liquidation_desk_notes"]},
            payload.changed_by,
        )

    if payload.funding_entity_name is not None or payload.funding_entity_kyc_complete:
        investor = commitment.get("investors") or {}
        if isinstance(investor, list):
            investor = investor[0] if investor else {}
        funding_fields = build_funding_source_fields(
            investor.get("entity_name"),
            payload.funding_entity_name if payload.funding_entity_name is not None else commitment.get("funding_entity_name"),
            current_kyc_status=commitment.get("funding_entity_kyc_status"),
            ops_mark_kyc_complete=bool(payload.funding_entity_kyc_complete),
        )
        updates.update(funding_fields)
        _log_event(
            firm_id,
            commitment_id,
            "funding_source_updated",
            {
                "funding_entity_name": commitment.get("funding_entity_name"),
                "funding_entity_kyc_status": commitment.get("funding_entity_kyc_status"),
            },
            funding_fields,
            payload.changed_by,
        )

    if not updates:
        return {"status": "no_changes", "commitment_id": commitment_id}

    updated = supabase.table("commitments").update(updates).eq("id", commitment_id).execute()
    from core.pw_liquidation import recompute_commitment_pw_liquidation

    recompute_commitment_pw_liquidation(commitment_id, firm_id)
    return {"status": "updated", "commitment": updated.data[0]}


@router.patch("/{commitment_id}/fund")
def mark_funded(
    commitment_id: str,
    funded_amount: float,
    funding_entity_name: Optional[str] = Query(
        default=None,
        description="Legal entity name on the inbound wire (where capital was sent FROM). Compared to KYC subscriber.",
    ),
    funding_entity_kyc_complete: bool = Query(
        default=False,
        description="Set true when alternate-entity KYC is on file for a non-matching funding entity.",
    ),
    x_firm_id: Optional[str] = Header(default=None),
    changed_by: Optional[str] = None,
):
    """
    Mark a commitment as fully funded when the inbound subscription wire is confirmed received.
    Optionally record funding_entity_name (must match KYC subscriber or complete alt-entity KYC).
    Sends an automated funding confirmation email to the investor (cc advisor).
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    _log_event(
        firm_id, commitment_id, "wire_received",
        {"wire_status": commitment.get("wire_status"), "funded_amount": commitment.get("funded_amount")},
        {"wire_status": "Funded", "funded_amount": funded_amount},
        changed_by,
    )

    fund_updates: dict = {
        "wire_status": "Funded",
        "funded_amount": funded_amount,
    }
    if funding_entity_name is not None:
        investor = commitment.get("investors") or {}
        if isinstance(investor, list):
            investor = investor[0] if investor else {}
        fund_updates.update(
            build_funding_source_fields(
                investor.get("entity_name"),
                funding_entity_name,
                current_kyc_status=commitment.get("funding_entity_kyc_status"),
                ops_mark_kyc_complete=funding_entity_kyc_complete,
            )
        )

    updated = supabase.table("commitments").update(fund_updates).eq("id", commitment_id).execute()

    # Send funding received confirmation email
    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})
    if investor.get("primary_email"):
        try:
            settings = (
                supabase.table("firm_settings")
                .select("*")
                .eq("firm_id", firm_id)
                .single()
                .execute()
                .data
            )
            from core.email_templates import build_funding_received_email
            from core.graph_client import send_email
            email = build_funding_received_email(
                entity_name=investor.get("entity_name", ""),
                offering_name=deal.get("offering_name", ""),
                funded_amount=funded_amount,
                ops_contact_email=settings.get("ops_mailbox"),
                firm_id=firm_id,
            )
            send_email(
                settings=settings,
                to=investor["primary_email"],
                cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
                subject=email["subject"],
                body=email["body"],
            )
        except Exception as e:
            logger.error("Failed to send funding confirmation email: %s", e)

    return {"status": "funded", "commitment": updated.data[0]}


class SendWireEarlyPayload(BaseModel):
    changed_by: Optional[str] = None


@router.post("/{commitment_id}/send-wire-early")
def send_wire_early(
    commitment_id: str,
    payload: SendWireEarlyPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Send wire instructions to the investor even if the Advisory Agreement is still pending.
    Email body includes a notice to sign the ADV agreement ASAP.
    Updates commitment tracker: wire_sent_at + wire_sent_adv_pending = true.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})

    if not investor.get("primary_email"):
        raise HTTPException(status_code=400, detail="Investor has no email address on file.")

    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )

    # Get the fund's wire instructions
    deal_record = (
        supabase.table("deals")
        .select("wire_instructions, offering_name")
        .eq("firm_id", firm_id)
        .eq("offering_name", deal.get("offering_name", ""))
        .single()
        .execute()
        .data
    )
    wire_instructions = deal_record.get("wire_instructions") if deal_record else None
    if not wire_instructions:
        raise HTTPException(status_code=400, detail="No wire instructions found for this fund. Add them via POST /deals/{id}/wire-instructions first.")

    wire_text = (
        "\n".join(f"  {k}: {v}" for k, v in wire_instructions.items())
        if isinstance(wire_instructions, dict)
        else str(wire_instructions)
    )

    adv_pending = commitment["docusign_status"] not in SIGNED_STATES

    wire_breakdown = compute_commitment_wire_breakdown(
        committed_amount=float(commitment.get("committed_amount") or 0),
        deal_id=commitment["deal_id"],
        firm_id=firm_id,
    )

    from core.email_templates import build_wire_early_email
    from core.graph_client import send_email
    email = build_wire_early_email(
        entity_name=investor.get("entity_name", ""),
        offering_name=deal.get("offering_name", ""),
        committed_amount=float(commitment.get("committed_amount") or 0),
        wire_instructions=wire_text,
        adv_pending=adv_pending,
        ops_contact_email=settings.get("ops_mailbox"),
        wire_breakdown=wire_breakdown,
        firm_id=firm_id,
    )
    send_email(
        settings=settings,
        to=investor["primary_email"],
        cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
        subject=email["subject"],
        body=email["body"],
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    supabase.table("commitments").update({
        "wire_sent_at": now_iso,
        "wire_sent_adv_pending": adv_pending,
    }).eq("id", commitment_id).execute()

    _log_event(
        firm_id, commitment_id, "wire_instructions_sent_early",
        {},
        {"wire_sent_at": now_iso, "adv_pending": adv_pending},
        payload.changed_by,
    )

    return {
        "status": "wire_instructions_sent",
        "commitment_id": commitment_id,
        "adv_pending": adv_pending,
        "sent_to": investor["primary_email"],
        "wire_breakdown": wire_breakdown,
    }


@router.get("/{commitment_id}/wire-breakdown")
def get_commitment_wire_breakdown(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Returns subscription commitment plus any deal-level third-party fees allocated to this investor.
    Use for ops QA, advisor questions, and portal display. Carry is disclosure-only (not in wire sum).
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)
    return compute_commitment_wire_breakdown(
        committed_amount=float(commitment.get("committed_amount") or 0),
        deal_id=commitment["deal_id"],
        firm_id=firm_id,
    )


@router.post("/{commitment_id}/extract-wire")
def extract_wire_manual(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Download the commitment's signed subscription PDF from DocuSign and run wire extraction (GPT-4o vision).
    Does not require firm_settings.wire_extraction_enabled (that gate applies only to the webhook automation).
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)
    envelope_id = commitment.get("envelope_id")
    if not envelope_id:
        raise HTTPException(
            status_code=400,
            detail="Commitment has no envelope_id — wire extraction requires a DocuSign subscription envelope.",
        )
    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not settings:
        raise HTTPException(status_code=404, detail="Firm settings not found.")

    from core.docusign_client import download_signed_documents
    from core.wire_extractor import extract_wire_from_pdf

    pdf_bytes = download_signed_documents(envelope_id, settings)
    return extract_wire_from_pdf(
        commitment["investor_id"],
        commitment_id,
        firm_id,
        pdf_bytes,
        settings,
    )


@router.get("/{commitment_id}/prefill-preview")
def get_prefill_preview(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Return all pending KYC-extracted fields awaiting ops confirmation.
    Ops reviews each field and either accepts or overrides before sub docs are sent.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)
    investor_id = commitment["investor_id"]

    pending_changes = (
        supabase.table("investor_pending_changes")
        .select("id, field_name, proposed_value, source_doc_url, created_at")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("source", "kyc_extraction")
        .eq("status", "Pending")
        .order("created_at", desc=False)
        .execute()
        .data
    )

    investor = (
        supabase.table("investors")
        .select("entity_name, entity_type, mailing_address, tax_id, state_of_formation, kyc_status")
        .eq("id", investor_id)
        .single()
        .execute()
        .data
    )

    return {
        "commitment_id": commitment_id,
        "investor_id": investor_id,
        "investor": investor,
        "pending_fields": pending_changes,
        "ready_to_send": len(pending_changes) == 0,
    }


class PrefillDecision(BaseModel):
    field_change_id: str
    action: str  # "accept" | "override"
    override_value: Optional[str] = None


class ConfirmPrefillPayload(BaseModel):
    decisions: list[PrefillDecision]
    confirmed_by: Optional[str] = None


@router.post("/{commitment_id}/confirm-prefill")
def confirm_prefill(
    commitment_id: str,
    payload: ConfirmPrefillPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Ops confirms or overrides each pending KYC-extracted field.
    After all pending fields are resolved, sub docs are dispatched automatically.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)
    investor_id = commitment["investor_id"]

    investor_updates: dict = {}

    for decision in payload.decisions:
        change = (
            supabase.table("investor_pending_changes")
            .select("*")
            .eq("id", decision.field_change_id)
            .eq("investor_id", investor_id)
            .single()
            .execute()
            .data
        )
        if not change:
            raise HTTPException(
                status_code=404, detail=f"Pending change {decision.field_change_id} not found."
            )

        if decision.action == "accept":
            investor_updates[change["field_name"]] = change["proposed_value"]
        elif decision.action == "override":
            if decision.override_value is not None:
                investor_updates[change["field_name"]] = decision.override_value

        supabase.table("investor_pending_changes").update({
            "status": "Confirmed" if decision.action == "accept" else "Overridden",
            "confirmed_by": payload.confirmed_by,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", decision.field_change_id).execute()

    if investor_updates:
        supabase.table("investors").update(investor_updates).eq("id", investor_id).execute()

    # Check if all pending fields have been resolved
    remaining = (
        supabase.table("investor_pending_changes")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("source", "kyc_extraction")
        .eq("status", "Pending")
        .limit(1)
        .execute()
        .data
    )

    if remaining:
        return {
            "status": "partial_review",
            "commitment_id": commitment_id,
            "message": "Some fields still pending confirmation.",
        }

    # All fields confirmed — send sub docs automatically
    try:
        from core.database import supabase as _db
        from core.docusign_client import send_envelope
        from core.email_templates import build_docusign_dispatch_email
        from core.graph_client import send_email

        settings_result = _db.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
        settings = settings_result.data or {}

        investor_record = _db.table("investors").select("*").eq("id", investor_id).single().execute().data
        deal_record = _db.table("deals").select("*").eq("id", commitment["deal_id"]).single().execute().data

        if not investor_record or not deal_record:
            raise HTTPException(status_code=500, detail="Could not load investor or deal for envelope send.")

        # Only send if envelope hasn't already been sent
        if not commitment.get("envelope_id"):
            envelope_result = send_envelope(
                settings=settings,
                investor=investor_record,
                deal=deal_record,
                commitment=commitment,
            )
            envelope_id = envelope_result["envelope_id"]
            supabase.table("commitments").update({
                "envelope_id": envelope_id,
                "docusign_status": "Sent",
            }).eq("id", commitment_id).execute()

            if investor_record.get("primary_email"):
                email1 = build_docusign_dispatch_email(
                    entity_name=investor_record["entity_name"],
                    offering_name=deal_record["offering_name"],
                    committed_amount=float(commitment.get("committed_amount") or 0),
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

            return {
                "status": "sub_docs_sent",
                "commitment_id": commitment_id,
                "envelope_id": envelope_id,
                "message": "All fields confirmed. Sub docs dispatched.",
            }
        else:
            return {
                "status": "already_sent",
                "commitment_id": commitment_id,
                "envelope_id": commitment["envelope_id"],
                "message": "Sub docs were already sent.",
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pre-fill confirmed but envelope send failed: {e}")


class SideLetterPayload(BaseModel):
    provisions: list[str]
    ppm_section_reference: Optional[str] = None
    override_mgmt_fee: Optional[float] = None
    override_carry: Optional[float] = None
    drafted_by: Optional[str] = None


@router.post("/{commitment_id}/side-letter")
def draft_side_letter(
    commitment_id: str,
    payload: SideLetterPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    GPT-draft a side letter for this commitment using the firm's template.
    Returns the completed text for ops review — nothing is sent or saved yet.
    Call POST /side-letter/attach once ops approves.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    investor = (
        supabase.table("investors")
        .select("*")
        .eq("id", commitment["investor_id"])
        .single()
        .execute()
        .data
    )
    deal = (
        supabase.table("deals")
        .select("*")
        .eq("id", commitment["deal_id"])
        .single()
        .execute()
        .data
    )

    if not investor or not deal:
        raise HTTPException(status_code=404, detail="Investor or deal not found.")

    try:
        from core.side_letter import generate_side_letter
        draft_text = generate_side_letter(
            firm_id=firm_id,
            investor=investor,
            deal=deal,
            commitment=commitment,
            provisions=payload.provisions,
            ppm_section_reference=payload.ppm_section_reference,
            override_mgmt_fee=payload.override_mgmt_fee,
            override_carry=payload.override_carry,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Persist the draft and provisions to commitment for the attach step
    supabase.table("commitments").update({
        "side_letter_notes": draft_text,
        "side_letter_provisions": {
            "provisions": payload.provisions,
            "ppm_section_reference": payload.ppm_section_reference,
            "override_mgmt_fee": payload.override_mgmt_fee,
            "override_carry": payload.override_carry,
        },
        "side_letter_generated_at": datetime.now(timezone.utc).isoformat(),
        "has_side_letter": True,
        "override_mgmt_fee": payload.override_mgmt_fee,
        "override_carry": payload.override_carry,
    }).eq("id", commitment_id).execute()

    _log_event(firm_id, commitment_id, "side_letter_drafted", {}, {"provisions": payload.provisions}, payload.drafted_by)

    return {
        "status": "draft_ready",
        "commitment_id": commitment_id,
        "investor": investor["entity_name"],
        "offering": deal["offering_name"],
        "draft": draft_text,
        "next_step": f"POST /commitments/{commitment_id}/side-letter/attach to prepend to DocuSign envelope",
    }


@router.get("/{commitment_id}/side-letter/preview")
def get_side_letter_preview(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Return the most recently drafted side letter text for ops review."""
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    if not commitment.get("side_letter_notes"):
        raise HTTPException(
            status_code=404,
            detail="No side letter draft found. Call POST /side-letter first.",
        )

    return {
        "commitment_id": commitment_id,
        "has_side_letter": commitment.get("has_side_letter", False),
        "generated_at": commitment.get("side_letter_generated_at"),
        "provisions": commitment.get("side_letter_provisions"),
        "draft": commitment.get("side_letter_notes"),
    }


@router.post("/{commitment_id}/side-letter/attach")
def attach_side_letter(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Convert the approved side letter draft to PDF and inject it as the first
    document in the DocuSign envelope. The signing chain is unchanged —
    the side letter is a read-only attachment that signers see before the sub docs.

    This endpoint is only valid before the envelope has been sent.
    """
    firm_id = _require_firm(x_firm_id)
    commitment = _get_commitment(commitment_id, firm_id)

    if commitment.get("envelope_id"):
        raise HTTPException(
            status_code=409,
            detail="DocuSign envelope already sent. Side letter cannot be prepended after send.",
        )
    if not commitment.get("side_letter_notes"):
        raise HTTPException(
            status_code=422,
            detail="No side letter draft found. Call POST /side-letter first.",
        )

    investor = (
        supabase.table("investors").select("*").eq("id", commitment["investor_id"]).single().execute().data
    )
    deal = (
        supabase.table("deals").select("*").eq("id", commitment["deal_id"]).single().execute().data
    )
    if not investor or not deal:
        raise HTTPException(status_code=404, detail="Investor or deal not found.")

    settings = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute().data or {}

    from core.side_letter import side_letter_to_pdf_bytes
    pdf_bytes = side_letter_to_pdf_bytes(
        text=commitment["side_letter_notes"],
        entity_name=investor["entity_name"],
        offering_name=deal["offering_name"],
    )

    # Save to SharePoint
    side_letter_path = None
    folder_id = investor.get("sharepoint_folder_id")
    if folder_id and pdf_bytes:
        try:
            from core.docusign_client import truncate_entity_name
            from core.graph_client import save_document_to_folder
            entity_slug = truncate_entity_name(investor["entity_name"], max_len=35).replace(" ", "_")
            fund_slug = deal["offering_name"].replace(" ", "_")
            sl_filename = f"{entity_slug}_{fund_slug}_SideLetter.pdf"
            save_document_to_folder(settings, folder_id, sl_filename, pdf_bytes)
            side_letter_path = sl_filename
        except Exception as e:
            logger.warning("Side letter SharePoint save failed: %s", e)

    # Send the envelope with side letter prepended
    from core.docusign_client import send_envelope
    try:
        envelope_result = send_envelope(
            settings=settings,
            investor=investor,
            deal=deal,
            commitment=commitment,
            side_letter_pdf=pdf_bytes,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DocuSign send failed: {e}")

    supabase.table("commitments").update({
        "envelope_id": envelope_result["envelope_id"],
        "docusign_status": "Sent",
        "side_letter_pdf_path": side_letter_path,
    }).eq("id", commitment_id).execute()

    _log_event(
        firm_id, commitment_id, "side_letter_attached",
        {},
        {"envelope_id": envelope_result["envelope_id"], "side_letter_path": side_letter_path},
        None,
    )

    return {
        "status": "envelope_sent_with_side_letter",
        "commitment_id": commitment_id,
        "envelope_id": envelope_result["envelope_id"],
        "side_letter_path": side_letter_path,
    }


@router.get("/{commitment_id}/history")
def get_commitment_history(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Return the full event history for a commitment (audit trail)."""
    firm_id = _require_firm(x_firm_id)

    events = (
        supabase.table("commitment_events")
        .select("*")
        .eq("commitment_id", commitment_id)
        .eq("firm_id", firm_id)
        .order("changed_at", desc=False)
        .execute()
        .data
    )
    return {"commitment_id": commitment_id, "events": events}


@router.post("/{commitment_id}/portal-link")
def regenerate_portal_link(
    commitment_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Ops-only: generate (or regenerate) an investor portal access link.
    Revokes any existing active token for this commitment and issues a fresh one.
    Useful when a link expires or an investor misplaces their email.
    """
    from core.portal import generate_portal_token

    firm_id = _require_firm(x_firm_id)

    commitment = (
        supabase.table("commitments")
        .select("id, firm_id, investor_id")
        .eq("id", commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found.")

    settings = (
        supabase.table("firm_settings")
        .select("portal_link_expiry_days")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    ) or {}

    expiry_days = settings.get("portal_link_expiry_days") or 30

    result = generate_portal_token(
        firm_id=firm_id,
        investor_id=commitment["investor_id"],
        commitment_id=commitment_id,
        expiry_days=expiry_days,
    )

    _log_event(
        firm_id, commitment_id, "portal_link_regenerated",
        {},
        {"portal_url": result["portal_url"], "expires_at": result["expires_at"]},
        None,
    )

    return {
        "commitment_id": commitment_id,
        "portal_url": result["portal_url"],
        "expires_at": result["expires_at"],
    }
