"""
Intake routes — ingest new deal onboarding requests from multiple sources:
  POST /intake/email           — Microsoft Graph change notification (advisor emails)
  POST /intake/form            — Power Automate POST from Microsoft Forms submission
  POST /intake/prospect        — Advisor self-service prospect intake (no ops required)
  GET  /intake/prospect/pending — Latest advisor prospect payload for ops drawer prefill
  GET  /intake/email-queue     — Low-confidence email parses awaiting ops triage
  POST /intake/email-review/{id}/approve — Onboard from queued email parse
  POST /intake/email-review/{id}/reject  — Dismiss queued email parse
  GET  /intake/form-config     — Returns active deals list for dynamic form dropdowns
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from core.ai_parser import parse_email, parse_form_submission
from core.auth import intake_key_limiter, resolve_firm_from_intake_key
from core.database import supabase
from core.investor_lookup import normalize_email, pick_investor_for_email_intake
from core.onboarding import run_onboarding

router = APIRouter()

GRAPH_VALIDATION_TOKEN_HEADER = "validationToken"
logger = logging.getLogger(__name__)


def _get_firm_settings_by_mailbox(ops_mailbox: str) -> tuple[str, dict]:
    result = (
        supabase.table("firm_settings")
        .select("firm_id, *")
        .eq("ops_mailbox", ops_mailbox)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"No firm found for mailbox: {ops_mailbox}")
    return result.data["firm_id"], result.data


def _graph_client_state_is_valid(notification: dict, settings: dict) -> bool:
    expected = settings.get("graph_subscription_client_state")
    actual = notification.get("clientState")
    if expected and actual and secrets.compare_digest(str(actual), str(expected)):
        return True

    logger.warning(
        "[IntakeEmail] Skipping notification with invalid Microsoft Graph clientState for firm_id=%s",
        settings.get("firm_id"),
    )
    return False


def _validate_body_firm(body_firm_id: Optional[str], key_firm_id: str) -> None:
    if body_firm_id and body_firm_id != key_firm_id:
        raise HTTPException(status_code=422, detail="firm_id does not match intake key.")


@router.post("/email")
async def intake_email(request: Request):
    """
    Receive Microsoft Graph change notification for new emails to the ops mailbox.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=validation_token)

    body = await request.json()
    notifications = body.get("value", [])

    results = []
    for notification in notifications:
        resource = notification.get("resource", "")
        parts = resource.split("/")
        if len(parts) < 4:
            continue

        ops_mailbox = parts[1]
        message_id = parts[-1]

        try:
            firm_id, settings = _get_firm_settings_by_mailbox(ops_mailbox)
        except HTTPException:
            continue
        if not _graph_client_state_is_valid(notification, settings):
            continue

        from core.graph_client import get_email_message

        msg = get_email_message(settings, message_id)
        raw_text = msg.get("body", "")
        if not raw_text.strip():
            continue

        parsed = parse_email(raw_text)
        confidence = str(parsed.get("confidence") or "medium").lower()

        if confidence == "low":
            row = (
                supabase.table("intake_email_review")
                .insert({
                    "firm_id": firm_id,
                    "message_id": message_id,
                    "subject": msg.get("subject"),
                    "from_address": msg.get("from_address"),
                    "raw_body": raw_text[:8000],
                    "parsed_payload": parsed,
                    "confidence": confidence,
                    "status": "Pending",
                })
                .execute()
            )
            review_id = (row.data or [{}])[0].get("id")
            results.append({
                "status": "queued_for_review",
                "review_id": review_id,
                "investor_name": parsed.get("investor_name"),
            })
            continue

        existing = pick_investor_for_email_intake(
            firm_id,
            parsed.get("investor_email") or msg.get("from_address"),
            parsed.get("investor_name"),
        )
        if existing:
            parsed["investor_name"] = existing["entity_name"]
            parsed["investor_email"] = existing.get("primary_email") or parsed.get("investor_email")
            parsed["entity_type"] = parsed.get("entity_type") or existing.get("entity_type")

        result = run_onboarding(firm_id=firm_id, payload=parsed)
        results.append({"status": "onboarded", "matched_existing": bool(existing), **result})

    return {"processed": len(results), "results": results}


class InterestedParty(BaseModel):
    """CPA, attorney, family office contact, etc. that should receive copies of statements."""
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None  # e.g., "CPA", "Attorney", "Family Office"
    receives_statements: bool = False


class FormSubmissionPayload(BaseModel):
    firm_id: Optional[str] = None
    investor_name: str
    fund_name: str
    committed_amount: float
    advisor_email: Optional[str] = None
    investor_email: Optional[str] = None
    entity_type: Optional[str] = None
    advisor_id: Optional[str] = None
    notes: Optional[str] = None
    country_of_formation: Optional[str] = None
    # Orion householding declaration — advisor must answer at intake
    orion_is_new_household: bool = False
    orion_linked_household_name: Optional[str] = None
    # Advisory fee — defaults to 1%; advisor only needs to fill this in if changing it
    advisory_fee_pct: float = 1.0
    # Identity (required for Orion NAImport on close)
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None  # SSN | EIN | ITIN | Foreign
    mailing_address: Optional[str] = None
    phone: Optional[str] = None
    date_of_birth: Optional[str] = None  # YYYY-MM-DD
    # Joint / trust / entity co-clients
    client_one_name: Optional[str] = None
    client_one_email: Optional[str] = None
    client_one_phone: Optional[str] = None
    client_one_dob: Optional[str] = None
    client_one_ssn_last4: Optional[str] = None
    client_two_name: Optional[str] = None
    client_two_email: Optional[str] = None
    client_two_phone: Optional[str] = None
    client_two_dob: Optional[str] = None
    client_two_ssn_last4: Optional[str] = None
    # Interested parties (CPAs, attorneys, etc.)
    interested_parties: list[InterestedParty] = []
    # Communication preferences
    preferred_contact_method: Optional[str] = None  # email | phone | mail | advisor_only
    no_electronic_access: bool = False
    prefers_physical_mail: bool = False
    do_not_contact: bool = False
    # Compliance
    accredited_investor: Optional[bool] = None
    qualified_purchaser: Optional[bool] = None
    # Ops notes
    internal_notes: Optional[str] = None


@router.post("/form")
@intake_key_limiter.limit("60/minute")
async def intake_form(
    request: Request,
    payload: FormSubmissionPayload,
    x_pivot_intake_key: Optional[str] = Header(default=None),
):
    """
    Receive a structured Microsoft Form submission via Power Automate.
    Fires the same onboarding pipeline. Includes Orion householding declaration.
    """
    firm_id = resolve_firm_from_intake_key(x_pivot_intake_key or "")
    _validate_body_firm(payload.firm_id, firm_id)

    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not settings:
        raise HTTPException(status_code=404, detail="Firm not found.")

    payload_data = payload.model_dump()
    payload_data["firm_id"] = firm_id
    parsed = parse_form_submission(payload_data)
    # Forward every advisor-supplied field onto the onboarding payload so
    # run_onboarding can persist them to investors / commitments.
    for field in (
        "advisor_id",
        "orion_is_new_household",
        "orion_linked_household_name",
        "country_of_formation",
        "advisory_fee_pct",
        "tax_id",
        "tax_id_type",
        "mailing_address",
        "phone",
        "date_of_birth",
        "client_one_name", "client_one_email", "client_one_phone", "client_one_dob", "client_one_ssn_last4",
        "client_two_name", "client_two_email", "client_two_phone", "client_two_dob", "client_two_ssn_last4",
        "interested_parties",
        "preferred_contact_method",
        "no_electronic_access",
        "prefers_physical_mail",
        "do_not_contact",
        "accredited_investor",
        "qualified_purchaser",
        "internal_notes",
    ):
        parsed[field] = payload_data.get(field)

    result = run_onboarding(firm_id=firm_id, payload=parsed)
    return {"status": "onboarded", **result}


class WireInstructionsInput(BaseModel):
    """Optional inbound wire instructions if advisor already has them at intake."""
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    routing_number: Optional[str] = None
    swift_code: Optional[str] = None
    bank_address: Optional[str] = None
    reference: Optional[str] = None


class ProspectIntakePayload(BaseModel):
    """
    Advisor self-service prospect intake. Fires full onboarding automatically.
    Requires the advisor to declare Orion householding upfront.

    Field groups (matches the Altvio Advisor Portal intake form):
      - Deal: fund + commitment economics
      - Client 1 (primary): name, email, phone, entity classification
      - Client 2 (optional, for joint accounts): name, email, phone
      - Authorized Signer (optional, for entities/trusts): captured via
        interested_parties with role='Authorized Signer'
      - Side letter request + terms
      - Interested parties (CPAs, attorneys, etc.)
      - Optional wire instructions
      - Orion householding declaration
    """
    firm_id: Optional[str] = None

    # --- Deal selection
    fund_name: str
    committed_amount: float
    advisory_fee_pct: float = 1.0

    # --- Primary client (Client 1)
    investor_name: str          # entity_name on the record
    investor_email: str
    phone: Optional[str] = None
    entity_type: str
    country_of_formation: Optional[str] = None
    state_of_formation: Optional[str] = None

    # --- Joint co-client (Client 2) — only set for joint accounts
    client_two_name: Optional[str] = None
    client_two_email: Optional[str] = None
    client_two_phone: Optional[str] = None

    # --- Authorized signer (for trusts, LLCs, LPs, corporations) — captured as
    # an interested_party with role="Authorized Signer" inside interested_parties.

    # --- Side letter request
    side_letter_requested: bool = False
    side_letter_terms: Optional[str] = None

    # --- Interested parties (CPAs, attorneys, authorized signers, family office)
    interested_parties: list[InterestedParty] = []

    # --- Optional wire instructions (advisor may have them at intake)
    wire_instructions: Optional[WireInstructionsInput] = None

    # --- Advisor identity
    advisor_email: str

    # --- Orion householding (required answer)
    orion_is_new_household: bool = False
    orion_linked_household_name: Optional[str] = None

    # --- Free-form
    notes: Optional[str] = None


@router.post("/prospect")
@intake_key_limiter.limit("60/minute")
async def intake_prospect(
    request: Request,
    payload: ProspectIntakePayload,
    x_pivot_intake_key: Optional[str] = Header(default=None),
):
    """
    Advisor self-service: add a new prospect to an active fund without ops involvement.
    Automatically fires sub docs, KYC email, SharePoint folder provisioning.
    Ops is notified via email when a new prospect is submitted.
    """
    firm_id = resolve_firm_from_intake_key(x_pivot_intake_key or "")
    _validate_body_firm(payload.firm_id, firm_id)

    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not settings:
        raise HTTPException(status_code=404, detail="Firm not found.")

    # Validate the fund exists and is active
    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("firm_id", firm_id)
        .eq("offering_name", payload.fund_name)
        .eq("status", "Active")
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail=f"No active fund named '{payload.fund_name}' found.")

    prospect_payload = payload.model_dump()
    try:
        supabase.table("intake_prospects").insert({
            "firm_id": firm_id,
            "deal_id": deal["id"],
            "investor_email": normalize_email(payload.investor_email),
            "payload": prospect_payload,
            "status": "submitted",
        }).execute()
    except Exception as exc:
        logger.warning("Failed to stash intake prospect payload: %s", exc)

    onboarding_payload = {
        "investor_name": payload.investor_name,
        "fund_name": payload.fund_name,
        "committed_amount": payload.committed_amount,
        "advisor_email": payload.advisor_email,
        "investor_email": payload.investor_email,
        "entity_type": payload.entity_type,
        "country_of_formation": payload.country_of_formation,
        "state_of_formation": payload.state_of_formation,
        "phone": payload.phone,
        "confidence": "high",
        "orion_is_new_household": payload.orion_is_new_household,
        "orion_linked_household_name": payload.orion_linked_household_name,
        "advisory_fee_pct": payload.advisory_fee_pct,
        "notes": payload.notes,
        # Joint co-client
        "client_two_name": payload.client_two_name,
        "client_two_email": payload.client_two_email,
        "client_two_phone": payload.client_two_phone,
        # Interested parties (serialize Pydantic models → dicts)
        "interested_parties": [ip.model_dump() for ip in payload.interested_parties],
        # Side letter request flows to commitment
        "side_letter_requested": payload.side_letter_requested,
        "side_letter_terms": payload.side_letter_terms,
        # Optional wire instructions (saved to investors.wire_instructions JSONB)
        "wire_instructions": (
            payload.wire_instructions.model_dump(exclude_none=True)
            if payload.wire_instructions else None
        ),
    }

    result = run_onboarding(firm_id=firm_id, payload=onboarding_payload)

    try:
        pending_rows = (
            supabase.table("intake_prospects")
            .select("id")
            .eq("firm_id", firm_id)
            .eq("investor_email", normalize_email(payload.investor_email))
            .eq("status", "submitted")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if pending_rows:
            supabase.table("intake_prospects").update({
                "status": "processed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", pending_rows[0]["id"]).execute()
    except Exception as exc:
        logger.warning("Failed to mark intake prospect processed: %s", exc)

    # Notify ops of new prospect submission
    try:
        from core.graph_client import send_email
        orion_note = ""
        if payload.orion_is_new_household:
            orion_note = "\nOrion Household: NEW (advisor declared — bypass fuzzy match)"
        elif payload.orion_linked_household_name:
            orion_note = f"\nOrion Household: Link to existing '{payload.orion_linked_household_name}' (needs ops confirmation)"
        send_email(
            settings=settings,
            to=settings.get("ops_mailbox", ""),
            cc=[],
            subject=f"New Prospect Submitted — {payload.investor_name} / {payload.fund_name}",
            body=(
                f"Advisor {payload.advisor_email} has submitted a new prospect via the Advisor Portal.\n\n"
                f"Investor: {payload.investor_name}\n"
                f"Entity Type: {payload.entity_type}\n"
                f"Fund: {payload.fund_name}\n"
                f"Commitment: ${payload.committed_amount:,.2f}{orion_note}\n\n"
                f"Sub docs and KYC email have been automatically dispatched."
            ),
        )
    except Exception as e:
        logger.error("Failed to send intake prospect ops notification: %s", e)

    return {"status": "onboarded", "source": "advisor_prospect", **result}


@router.get("/prospect/pending")
def get_pending_prospect(
    x_firm_id: Optional[str] = Header(default=None),
    deal_id: str = Query(..., description="Active deal UUID"),
    email: str = Query(..., min_length=3),
):
    """
    Return the most recent advisor prospect submission for this fund + email.
    Used by the ops New Investor drawer to prefill fields from advisor intake.
    """
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    firm_id = x_firm_id
    needle = normalize_email(email)

    rows = (
        supabase.table("intake_prospects")
        .select("*")
        .eq("firm_id", firm_id)
        .eq("deal_id", deal_id)
        .eq("investor_email", needle)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return {"found": False, "prospect": None}

    row = rows[0]
    return {
        "found": True,
        "prospect": {
            "id": row.get("id"),
            "payload": row.get("payload"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "from_advisor": True,
        },
    }


@router.get("/email-queue")
def get_intake_email_queue(
    x_firm_id: Optional[str] = Header(default=None),
    status: str = Query(default="Pending"),
):
    """List email parses awaiting ops triage."""
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")

    query = (
        supabase.table("intake_email_review")
        .select("*")
        .eq("firm_id", x_firm_id)
        .order("created_at", desc=True)
    )
    if status.lower() != "all":
        query = query.eq("status", status)

    rows = query.execute().data or []
    return {"items": rows, "count": len(rows)}


class EmailReviewActionPayload(BaseModel):
    reviewed_by: Optional[str] = "ops"
    investor_id: Optional[str] = None


@router.post("/email-review/{review_id}/approve")
def approve_intake_email_review(
    review_id: str,
    payload: EmailReviewActionPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Approve a queued email parse and run onboarding."""
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")

    row = (
        supabase.table("intake_email_review")
        .select("*")
        .eq("id", review_id)
        .eq("firm_id", x_firm_id)
        .single()
        .execute()
        .data
    )
    if not row:
        raise HTTPException(status_code=404, detail="Review item not found.")
    if row.get("status") != "Pending":
        raise HTTPException(status_code=409, detail="Review item already resolved.")

    parsed: dict[str, Any] = dict(row.get("parsed_payload") or {})
    parsed["confidence"] = "high"

    if payload.investor_id:
        inv = (
            supabase.table("investors")
            .select("id, entity_name, primary_email, entity_type")
            .eq("id", payload.investor_id)
            .eq("firm_id", x_firm_id)
            .single()
            .execute()
            .data
        )
        if inv:
            parsed["investor_name"] = inv["entity_name"]
            parsed["investor_email"] = inv.get("primary_email")
            parsed["entity_type"] = parsed.get("entity_type") or inv.get("entity_type")
    else:
        existing = pick_investor_for_email_intake(
            x_firm_id,
            parsed.get("investor_email") or row.get("from_address"),
            parsed.get("investor_name"),
        )
        if existing:
            parsed["investor_name"] = existing["entity_name"]
            parsed["investor_email"] = existing.get("primary_email")
            payload.investor_id = existing["id"]

    if not parsed.get("fund_name") or not parsed.get("committed_amount"):
        raise HTTPException(
            status_code=422,
            detail="Parsed payload missing fund_name or committed_amount. Edit and resubmit via ops drawer.",
        )

    result = run_onboarding(firm_id=x_firm_id, payload=parsed)

    supabase.table("intake_email_review").update({
        "status": "Approved",
        "matched_investor_id": payload.investor_id or result.get("investor_id"),
        "reviewed_by": payload.reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", review_id).execute()

    return {"status": "onboarded", **result}


@router.post("/email-review/{review_id}/reject")
def reject_intake_email_review(
    review_id: str,
    payload: EmailReviewActionPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Dismiss a queued email parse without onboarding."""
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")

    supabase.table("intake_email_review").update({
        "status": "Rejected",
        "reviewed_by": payload.reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", review_id).eq("firm_id", x_firm_id).execute()

    return {"status": "rejected", "review_id": review_id}


@router.get("/form-config")
def get_form_config(firm_id: str):
    """
    Returns the current list of active funds for dynamic intake form dropdowns.
    Called by the intake form on load to always reflect live deal data.
    """
    deals = (
        supabase.table("deals")
        .select("id, offering_name, target_raise, fund_manager")
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return {
        "active_funds": [
            {"id": d["id"], "name": d["offering_name"], "fund_manager": d.get("fund_manager")}
            for d in deals
        ],
        "entity_types": ["Individual", "LLC", "Trust", "LP", "Corporation", "Other"],
        "orion_household_question": "Is this investor already in Orion under a different name or entity?",
        "orion_household_options": [
            {"value": False, "label": "No — create a new Orion household for this investor"},
            {"value": True, "label": "Yes — this links to an existing Orion household (I will provide the name)"},
        ],
    }
