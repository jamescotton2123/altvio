"""
Firm Settings — configuration endpoints for all three portal visibility surfaces.

PATCH /firm/settings/portal-visibility   — investor portal section toggles
PATCH /firm/settings/exec-config         — redirect alias (canonical is PATCH /exec/config)
GET   /firm/settings                     — read full firm_settings record
PATCH /firm/settings/ops-alerts         — fee expiry digest + firm follow-up playbook; optional statement mailing unconfirmed alerts
PATCH /firm/settings/trader-digest      — optional daily email to desks (liquidation queue)
PATCH /firm/settings/docusign-tax-forms — DocuSign W-9 / W-8BEN / W-8BEN-E template IDs
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.database import supabase
from core.fee_expiry_digest import FEE_EXPIRY_FOLLOWUP_STYLES

router = APIRouter()

PORTAL_VISIBILITY_FIELDS = [
    "show_documents",
    "show_capital_account",
    "show_distributions",
    "show_distribution_history",
    "show_loi_opportunities",
    "show_wire_change_request",
]

PORTAL_VISIBILITY_DESCRIPTIONS = {
    "show_documents":           "Signed subscription agreements, side letters, wire PDFs",
    "show_capital_account":     "Per-fund committed/funded summary table",
    "show_distributions":       "Distribution amounts received per fund",
    "show_distribution_history":"Full distribution history table",
    "show_loi_opportunities":   "Open funds available for new LOI submission",
    "show_wire_change_request": "Wire instruction update form",
}


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


# ---------------------------------------------------------------------------
# GET /firm/settings
# ---------------------------------------------------------------------------

@router.get("")
def get_firm_settings(x_firm_id: Optional[str] = Header(default=None)):
    """Return the full firm_settings record."""
    firm_id = _require_firm(x_firm_id)
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


# ---------------------------------------------------------------------------
# PATCH /firm/settings/portal-visibility
# ---------------------------------------------------------------------------

class PortalVisibilityPayload(BaseModel):
    show_documents: Optional[bool] = None
    show_capital_account: Optional[bool] = None
    show_distributions: Optional[bool] = None
    show_distribution_history: Optional[bool] = None
    show_loi_opportunities: Optional[bool] = None
    show_wire_change_request: Optional[bool] = None


@router.patch("/portal-visibility")
def update_portal_visibility(
    payload: PortalVisibilityPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Control which sections investors see on their document portal.
    Returns the full updated visibility config with descriptions of each section.
    """
    firm_id = _require_firm(x_firm_id)

    settings = supabase.table("firm_settings").select("portal_visibility").eq("firm_id", firm_id).single().execute().data or {}
    current = settings.get("portal_visibility") or {}

    defaults = {f: True for f in PORTAL_VISIBILITY_FIELDS}
    updated = {**defaults, **current}

    changes = payload.model_dump(exclude_none=True)
    updated.update(changes)

    supabase.table("firm_settings").update({"portal_visibility": updated}).eq("firm_id", firm_id).execute()

    return {
        "status": "updated",
        "portal_visibility": {
            field: {
                "enabled": updated[field],
                "description": PORTAL_VISIBILITY_DESCRIPTIONS.get(field, ""),
            }
            for field in PORTAL_VISIBILITY_FIELDS
        },
    }


# ---------------------------------------------------------------------------
# PATCH /firm/settings/ops-alerts — scheduled ops notifications
# ---------------------------------------------------------------------------

class OpsAlertsPayload(BaseModel):
    notify_ops_fee_expiry: Optional[bool] = None
    fee_expiry_alert_days: Optional[int] = None
    fee_expiry_followup_style: Optional[str] = None
    fee_expiry_custom_instructions: Optional[str] = None
    notify_statement_mailing_unconfirmed: Optional[bool] = None
    wire_extraction_enabled: Optional[bool] = None


@router.patch("/ops-alerts")
def update_ops_alerts(
    payload: OpsAlertsPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Toggle daily ops email for expiring placement/upfront fee terms (08:00 job).

    fee_expiry_alert_days: include arrangements expiring within N days or already expired.

    fee_expiry_followup_style: optional playbook appended to digest + shown on GET /ops/todos:
      third_party_reminder (default), management_fee_transition, capital_call_planning,
      management_and_capital, custom_only (use with fee_expiry_custom_instructions).

    fee_expiry_custom_instructions: free-text notes always combined per style rules
    (e.g. appended after preset text, or alone when style is custom_only).

    notify_statement_mailing_unconfirmed: when true, Monday 08:00 job emails ops_mailbox for
      physical statement_mailings still unconfirmed 30+ days after mailed_date. Default off so
      firms that log mailings without chasing receipt receive no automated follow-up.

    wire_extraction_enabled: when true, DocuSign envelope-completed (subscription) runs GPT wire
      extraction from the signed PDF after Email 2 (non-blocking). Manual POST /commitments/{id}/extract-wire is always available.
    """
    firm_id = _require_firm(x_firm_id)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")
    if "fee_expiry_alert_days" in updates:
        d = int(updates["fee_expiry_alert_days"])
        if d < 1 or d > 365:
            raise HTTPException(status_code=400, detail="fee_expiry_alert_days must be between 1 and 365.")
        updates["fee_expiry_alert_days"] = d
    if "fee_expiry_followup_style" in updates:
        st = updates["fee_expiry_followup_style"].strip()
        if st not in FEE_EXPIRY_FOLLOWUP_STYLES:
            raise HTTPException(
                status_code=400,
                detail=f"fee_expiry_followup_style must be one of: {sorted(FEE_EXPIRY_FOLLOWUP_STYLES)}",
            )
        updates["fee_expiry_followup_style"] = st
    supabase.table("firm_settings").update(updates).eq("firm_id", firm_id).execute()
    return {"status": "updated", "updated_fields": list(updates.keys())}


# ---------------------------------------------------------------------------
# PATCH /firm/settings/trader-digest — desk liquidation email (08:00 job)
# ---------------------------------------------------------------------------

class TraderDigestPayload(BaseModel):
    notify_trader_liquidation_digest: Optional[bool] = None
    trader_liquidation_alert_days: Optional[int] = None


@router.patch("/trader-digest")
def update_trader_digest_settings(
    payload: TraderDigestPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    When notify_trader_liquidation_digest is true, the daily 08:00 job emails each active desk
    (traders with an api_key) their open private-wealth liquidation tickets in the alert window,
    and emails each Client Associate (investors.client_associate_email) their PW wire queue
    for Schwab / custody-initiated wires.
    """
    firm_id = _require_firm(x_firm_id)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")
    if "trader_liquidation_alert_days" in updates:
        d = int(updates["trader_liquidation_alert_days"])
        if d < 1 or d > 365:
            raise HTTPException(status_code=400, detail="trader_liquidation_alert_days must be between 1 and 365.")
        updates["trader_liquidation_alert_days"] = d
    supabase.table("firm_settings").update(updates).eq("firm_id", firm_id).execute()
    return {"status": "updated", "updated_fields": list(updates.keys())}


# ---------------------------------------------------------------------------
# PATCH /firm/settings/branding
# ---------------------------------------------------------------------------

class BrandingPayload(BaseModel):
    firm_name: Optional[str] = None
    brand_color: Optional[str] = None
    logo_url: Optional[str] = None
    portal_brand_tagline: Optional[str] = None
    portal_subdomain: Optional[str] = None
    ops_mailbox: Optional[str] = None
    wire_delivery_mode: Optional[str] = None
    portal_link_expiry_days: Optional[int] = None


@router.patch("/branding")
def update_firm_branding(
    payload: BrandingPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Update firm branding and portal configuration settings."""
    firm_id = _require_firm(x_firm_id)

    if payload.wire_delivery_mode and payload.wire_delivery_mode not in ("inline", "secure_link", "portal"):
        raise HTTPException(status_code=400, detail="wire_delivery_mode must be: inline | secure_link | portal")

    if payload.portal_subdomain:
        # Check subdomain isn't taken by another firm
        existing = (
            supabase.table("firm_settings")
            .select("firm_id")
            .eq("portal_subdomain", payload.portal_subdomain)
            .execute()
            .data
        )
        if existing and existing[0].get("firm_id") != firm_id:
            raise HTTPException(status_code=409, detail="This subdomain is already in use by another firm.")

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    supabase.table("firm_settings").update(updates).eq("firm_id", firm_id).execute()
    return {"status": "updated", "updated_fields": list(updates.keys())}


# ---------------------------------------------------------------------------
# PATCH /firm/settings/docusign-tax-forms — W-9 / W-8 template IDs for send_envelope
# ---------------------------------------------------------------------------

class DocusignTaxFormsPayload(BaseModel):
    docusign_w9_template_id: Optional[str] = None
    docusign_w8ben_template_id: Optional[str] = None
    docusign_w8bene_template_id: Optional[str] = None


@router.patch("/docusign-tax-forms")
def patch_docusign_tax_forms(
    payload: DocusignTaxFormsPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Persist DocuSign server template IDs for tax form composites (see core/docusign_client._resolve_tax_form_template_id)."""
    firm_id = _require_firm(x_firm_id)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")
    supabase.table("firm_settings").update(updates).eq("firm_id", firm_id).execute()
    return {"status": "updated", "updated_fields": list(updates.keys())}
