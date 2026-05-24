"""
Investor Portal — token generation, validation, document assembly, and capital account.

Two separate portal flows:
  1. KYC Upload Portal  (/portal/kyc/{token})
     - One-time upload link sent during onboarding instead of a raw SharePoint link
     - Entity-type-aware checklist of required documents
     - Files saved to SharePoint folder; KYC parser runs immediately
     - Works for investors AND third-party filers (attorneys, CPAs)
     - Token type: 'kyc', expires in 14 days

  2. Investor Document Portal  (/portal/view/{token})
     - Ongoing relationship portal for capital account, documents, distributions
     - Passwordless re-auth via subdomain login page (enter email → fresh link)
     - Token type: 'document', expires in 30 days with auto-renewal within 7 days
     - Self-service LOI submission and wire change requests

Subdomain model: each firm has {portal_subdomain}.pivotops.pro
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.database import supabase
from core.deal_fees import compute_commitment_wire_breakdown

# ---------------------------------------------------------------------------
# Required KYC documents by entity type
# ---------------------------------------------------------------------------

KYC_REQUIRED_DOCS: dict[str, list[str]] = {
    "Individual": [
        "Government-issued photo ID (passport preferred)",
        "W-9 — Request for Taxpayer Identification Number",
        "Accreditation evidence (income letter, CPA certification, or net worth statement)",
    ],
    "Joint": [
        "Government-issued photo ID for all parties",
        "W-9 for each party",
        "Accreditation evidence for all parties",
    ],
    "Revocable Trust": [
        "Trust Agreement or Certificate of Trust",
        "Photo ID for each trustee",
        "W-9 (signed by trustee)",
    ],
    "Irrevocable Trust": [
        "Trust Agreement or Certificate of Trust",
        "Photo ID for each trustee",
        "W-9 (signed by trustee)",
        "Beneficial Ownership Certification (if applicable)",
    ],
    "LLC": [
        "Articles of Organization or Certificate of Formation",
        "Operating Agreement",
        "EIN Confirmation Letter (IRS CP-575 or 147C)",
        "Beneficial Ownership Certification (members owning 25%+)",
        "Photo ID of authorized signer",
    ],
    "LP": [
        "Certificate of Limited Partnership",
        "Partnership Agreement",
        "EIN Confirmation Letter",
        "Photo ID of general partner / authorized signer",
    ],
    "Corporation": [
        "Articles of Incorporation",
        "Corporate Resolution authorizing the investment",
        "EIN Confirmation Letter",
        "Photo ID of authorized officer",
    ],
    "Foreign Individual": [
        "Passport (valid)",
        "W-8BEN — Certificate of Foreign Status",
        "Accreditation evidence (if applicable)",
    ],
    "Foreign Entity": [
        "Passport of authorized signers",
        "W-8BEN-E — Certificate of Foreign Status for Entities",
        "Certificate of Formation or Incorporation",
        "Organizational chart (required if there are nested entity layers)",
    ],
}

_DEFAULT_KYC_DOCS = [
    "Government-issued photo ID",
    "Tax certification (W-9 or W-8BEN)",
    "Accreditation or formation documents",
]


def _kyc_docs_for_entity(entity_type: str | None) -> list[str]:
    if not entity_type:
        return _DEFAULT_KYC_DOCS
    for key in KYC_REQUIRED_DOCS:
        if key.lower() in (entity_type or "").lower() or (entity_type or "").lower() in key.lower():
            return KYC_REQUIRED_DOCS[key]
    return _DEFAULT_KYC_DOCS


# ---------------------------------------------------------------------------
# Portal URL helpers
# ---------------------------------------------------------------------------

def _portal_base_url(settings: dict | None = None) -> str:
    """
    Returns the base URL for the portal, preferring firm subdomain if configured.
    Falls back to PORTAL_BASE_URL env var, then default.
    """
    subdomain = (settings or {}).get("portal_subdomain")
    if subdomain:
        return f"https://{subdomain}.pivotops.pro"
    return os.environ.get("PORTAL_BASE_URL", "https://portal.pivotops.pro")


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_portal_token(
    firm_id: str,
    investor_id: str,
    commitment_id: str,
    expiry_days: int = 30,
    settings: dict | None = None,
) -> dict:
    """
    Create a new investor document portal token (type='document').
    Revokes any existing active document tokens for the same commitment first.
    """
    supabase.table("portal_access_tokens").update({
        "revoked": True,
    }).eq("commitment_id", commitment_id).eq("revoked", False).eq("token_type", "document").execute()

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()

    supabase.table("portal_access_tokens").insert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "commitment_id": commitment_id,
        "token": token,
        "token_type": "document",
        "expires_at": expires_at,
    }).execute()

    base_url = _portal_base_url(settings)
    return {
        "token": token,
        "portal_url": f"{base_url}/view/{token}",
        "expires_at": expires_at,
    }


def generate_kyc_token(
    firm_id: str,
    investor_id: str,
    commitment_id: str,
    expiry_days: int = 14,
    settings: dict | None = None,
) -> dict:
    """
    Create a one-time KYC upload portal token (type='kyc').
    Replaces the raw SharePoint link sent in the KYC email.
    Expires in 14 days — it's a one-time task, not an ongoing access portal.
    """
    supabase.table("portal_access_tokens").update({
        "revoked": True,
    }).eq("commitment_id", commitment_id).eq("revoked", False).eq("token_type", "kyc").execute()

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()

    supabase.table("portal_access_tokens").insert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "commitment_id": commitment_id,
        "token": token,
        "token_type": "kyc",
        "expires_at": expires_at,
    }).execute()

    base_url = _portal_base_url(settings)
    return {
        "token": token,
        "kyc_url": f"{base_url}/kyc/{token}",
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def validate_portal_token(token: str, expected_type: str = "document") -> Optional[dict]:
    """
    Validate a portal token. Returns None if invalid, expired, revoked, or wrong type.
    Bumps access_count and records first access timestamp.
    Auto-renews document tokens that expire within 7 days.
    """
    result = (
        supabase.table("portal_access_tokens")
        .select("*, investors(*), commitments(*, deals(*))")
        .eq("token", token)
        .eq("revoked", False)
        .eq("token_type", expected_type)
        .single()
        .execute()
    )
    if not result.data:
        return None

    record = result.data
    expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        return None

    update: dict = {"access_count": record["access_count"] + 1}
    if not record.get("accessed_at"):
        update["accessed_at"] = datetime.now(timezone.utc).isoformat()

    # Auto-renew document tokens expiring within 7 days
    if expected_type == "document":
        days_left = (expires_at - datetime.now(timezone.utc)).days
        if days_left <= 7:
            new_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            update["expires_at"] = new_expiry
            record["expires_at"] = new_expiry

    supabase.table("portal_access_tokens").update(update).eq("id", record["id"]).execute()
    return record


def validate_kyc_token(token: str) -> Optional[dict]:
    return validate_portal_token(token, expected_type="kyc")


# ---------------------------------------------------------------------------
# Re-auth: investor requests a fresh magic link by email
# ---------------------------------------------------------------------------

def request_portal_access(email: str, firm_id: str, settings: dict) -> bool:
    """
    Passwordless re-auth flow. Investor enters their email at the firm's login page.
    Looks up the investor by primary_email + firm_id, generates a fresh document token,
    and emails the magic link. Returns True if found, False if not.
    """
    investor = (
        supabase.table("investors")
        .select("id, entity_name, primary_email")
        .eq("firm_id", firm_id)
        .eq("primary_email", email.strip().lower())
        .single()
        .execute()
        .data
    )
    if not investor:
        return False

    # Find the most recent active commitment for this investor
    commitment = (
        supabase.table("commitments")
        .select("id")
        .eq("firm_id", firm_id)
        .eq("investor_id", investor["id"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not commitment:
        return False

    commitment_id = commitment[0]["id"]
    expiry_days = settings.get("portal_link_expiry_days") or 30
    token_result = generate_portal_token(
        firm_id=firm_id,
        investor_id=investor["id"],
        commitment_id=commitment_id,
        expiry_days=expiry_days,
        settings=settings,
    )

    from core.graph_client import send_email
    firm_name = settings.get("firm_name", "Your Investment Manager")
    send_email(
        settings=settings,
        to=email,
        cc=[],
        subject=f"{firm_name} — Your Secure Portal Access Link",
        body=f"""Dear {investor['entity_name']},

You requested access to your secure investor portal. Click the link below to sign in:

  {token_result['portal_url']}

This link expires in {expiry_days} days. If you did not request this link, please contact our operations team.

{firm_name} Operations
""",
    )
    return True


# ---------------------------------------------------------------------------
# Capital account assembly (investor-scoped, all funds)
# ---------------------------------------------------------------------------

def assemble_capital_account(investor_id: str, firm_id: str) -> dict:
    """
    Build a full capital account view for an investor across all their commitments
    with this firm. Includes per-fund summary and distribution history.
    """
    commitments = (
        supabase.table("commitments")
        .select("*, deals(offering_name, fund_manager, close_date, status)")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
        .data
    ) or []

    distributions = (
        supabase.table("distribution_notices")
        .select("individual_amount, status, sent_at, distributions(distribution_date, distribution_type, offering_name)")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("status", "Sent")
        .order("created_at", desc=True)
        .execute()
        .data
    ) or []

    total_committed = sum(float(c.get("committed_amount") or 0) for c in commitments)
    total_funded = sum(float(c.get("funded_amount") or 0) for c in commitments)
    total_distributions = sum(float(d.get("individual_amount") or 0) for d in distributions)

    funds = []
    for c in commitments:
        deal = c.get("deals") or {}
        committed = float(c.get("committed_amount") or 0)
        funded = float(c.get("funded_amount") or 0)
        dist_for_fund = [
            d for d in distributions
            if (d.get("distributions") or {}).get("offering_name") == deal.get("offering_name")
        ]
        dist_total = sum(float(d.get("individual_amount") or 0) for d in dist_for_fund)
        wire_breakdown = compute_commitment_wire_breakdown(
            committed_amount=committed,
            deal_id=c["deal_id"],
            firm_id=firm_id,
        )
        funds.append({
            "commitment_id": c["id"],
            "offering_name": deal.get("offering_name", "—"),
            "fund_manager": deal.get("fund_manager", ""),
            "close_date": deal.get("close_date"),
            "deal_status": deal.get("status", ""),
            "committed_amount": committed,
            "funded_amount": funded,
            "total_wire_due": wire_breakdown.get("total_wire_due"),
            "third_party_fees_total": wire_breakdown.get("third_party_fees_total"),
            "wire_fee_lines": wire_breakdown.get("lines"),
            "carry_disclosures": wire_breakdown.get("carry_disclosures"),
            "wire_fee_warnings": wire_breakdown.get("warnings"),
            "distributions_received": dist_total,
            "net_position": funded - dist_total,
            "docusign_status": c.get("docusign_status", ""),
            "wire_status": c.get("wire_status", ""),
            "commitment_date": c.get("commitment_date"),
            "has_side_letter": bool(c.get("side_letter_pdf_path")),
            "envelope_id": c.get("envelope_id"),
        })

    dist_history = []
    for d in distributions:
        dist_data = d.get("distributions") or {}
        dist_history.append({
            "fund": dist_data.get("offering_name", "—"),
            "date": dist_data.get("distribution_date") or d.get("sent_at"),
            "type": dist_data.get("distribution_type", "Distribution"),
            "amount": float(d.get("individual_amount") or 0),
        })

    return {
        "summary": {
            "total_committed": total_committed,
            "total_funded": total_funded,
            "total_distributions": total_distributions,
            "net_position": total_funded - total_distributions,
            "fund_count": len(funds),
        },
        "funds": funds,
        "distribution_history": dist_history,
    }


# ---------------------------------------------------------------------------
# Open deals for LOI submission
# ---------------------------------------------------------------------------

def get_open_deals_for_investor(investor_id: str, firm_id: str) -> list[dict]:
    """
    Returns active deals the investor has NOT already committed to.
    Shown on the portal as 'Investment Opportunities'.
    """
    existing_deal_ids = set(
        c["deal_id"]
        for c in (
            supabase.table("commitments")
            .select("deal_id")
            .eq("investor_id", investor_id)
            .eq("firm_id", firm_id)
            .execute()
            .data or []
        )
    )

    open_deals = (
        supabase.table("deals")
        .select("id, offering_name, fund_manager, target_raise, close_date, strategy")
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    ) or []

    return [d for d in open_deals if d["id"] not in existing_deal_ids]


# ---------------------------------------------------------------------------
# Portal data assembly (document portal)
# ---------------------------------------------------------------------------

def _portal_visibility(settings: dict) -> dict:
    """
    Returns the firm's portal visibility config with safe defaults.
    All sections default to visible so existing firms without the config see everything.
    """
    defaults = {
        "show_documents": True,
        "show_capital_account": True,
        "show_distributions": True,
        "show_distribution_history": True,
        "show_loi_opportunities": True,
        "show_wire_change_request": True,
    }
    firm_config = settings.get("portal_visibility") or {}
    return {**defaults, **firm_config}


def assemble_portal_data(record: dict, settings: dict) -> dict:
    """
    Build the full data payload to render the investor document portal page.
    Token is commitment-scoped but page is investor-scoped — loads all commitments.
    Respects firm-level portal_visibility settings.
    """
    investor = record.get("investors") or {}
    commitment = record.get("commitments") or {}
    deal = (commitment.get("deals") or {}) if commitment else {}

    investor_id = record.get("investor_id")
    firm_id = record.get("firm_id")
    vis = _portal_visibility(settings)

    # Per-commitment documents (for the triggering commitment)
    documents = []
    if vis["show_documents"]:
        if commitment.get("envelope_id"):
            documents.append({
                "label": "Executed Subscription Agreement",
                "type": "subdoc",
                "fund": deal.get("offering_name", ""),
                "download_path": f"/portal/download/{record['token']}/subdoc",
            })
        if commitment.get("has_side_letter") and commitment.get("side_letter_pdf_path"):
            documents.append({
                "label": "Side Letter Agreement",
                "type": "side_letter",
                "fund": deal.get("offering_name", ""),
                "download_path": f"/portal/download/{record['token']}/side_letter",
            })
        wire_pdf = deal.get("wire_instructions_pdf_filename")
        if wire_pdf:
            documents.append({
                "label": "Wire Instructions (Official Bank Document)",
                "type": "wire_pdf",
                "fund": deal.get("offering_name", ""),
                "download_path": f"/portal/download/{record['token']}/wire_pdf",
            })
    else:
        wire_pdf = None

    wire_mode = settings.get("wire_delivery_mode", "inline")
    wire_data = deal.get("wire_instructions")

    # Capital account (all funds) — only if visible
    capital_account = None
    if vis["show_capital_account"] and investor_id and firm_id:
        raw = assemble_capital_account(investor_id, firm_id)
        # Strip distributions from each fund row if firm hides them
        if not vis["show_distributions"]:
            for f in raw.get("funds", []):
                f["distributions_received"] = None
                f["net_position"] = None
            raw["summary"]["total_distributions"] = None
            raw["summary"]["net_position"] = None
        # Strip distribution history if hidden
        if not vis["show_distribution_history"]:
            raw["distribution_history"] = []
        capital_account = raw

    # Open deals for LOI — only if visible
    open_deals = []
    if vis["show_loi_opportunities"] and investor_id and firm_id:
        open_deals = get_open_deals_for_investor(investor_id, firm_id)

    # Pending wire change check
    pending_wire_change = False
    if investor_id and firm_id:
        pending_wire_change = bool(
            supabase.table("investor_pending_changes")
            .select("id")
            .eq("investor_id", investor_id)
            .eq("firm_id", firm_id)
            .eq("field_name", "wire_instructions")
            .eq("status", "Pending")
            .execute()
            .data
        )

    return {
        "firm": {
            "name": settings.get("firm_name", "Your Investment Manager"),
            "logo_url": settings.get("logo_url"),
            "brand_color": settings.get("brand_color", "#0f172a"),
            "tagline": settings.get("portal_brand_tagline", "Secure Investor Document Access"),
            "ops_email": settings.get("ops_mailbox", ""),
        },
        "investor": {
            "entity_name": investor.get("entity_name", ""),
            "entity_type": investor.get("entity_type", ""),
        },
        "documents": documents,
        "wire": {
            "mode": wire_mode,
            "inline_text": _format_wire_inline(wire_data) if wire_mode == "inline" else None,
            "has_pdf": bool(deal.get("wire_instructions_pdf_filename")),
        },
        "capital_account": capital_account,
        "open_deals": open_deals,
        "pending_wire_change": pending_wire_change,
        "visibility": vis,
        "token": record["token"],
        "expires_at": record["expires_at"],
    }


def assemble_kyc_portal_data(record: dict, settings: dict) -> dict:
    """Build data payload to render the KYC upload portal page."""
    investor = record.get("investors") or {}
    commitment = record.get("commitments") or {}
    deal = (commitment.get("deals") or {}) if commitment else {}

    entity_type = investor.get("entity_type")
    required_docs = _kyc_docs_for_entity(entity_type)

    return {
        "firm": {
            "name": settings.get("firm_name", "Your Investment Manager"),
            "logo_url": settings.get("logo_url"),
            "brand_color": settings.get("brand_color", "#0f172a"),
            "ops_email": settings.get("ops_mailbox", ""),
        },
        "investor": {
            "entity_name": investor.get("entity_name", ""),
            "entity_type": entity_type or "Individual",
        },
        "fund": {
            "offering_name": deal.get("offering_name", ""),
        },
        "required_docs": required_docs,
        "token": record["token"],
        "expires_at": record["expires_at"],
    }


# ---------------------------------------------------------------------------
# Inline wire formatter
# ---------------------------------------------------------------------------

def _format_wire_inline(wi) -> str:
    if not wi:
        return "Contact operations for wire instructions."
    if isinstance(wi, str):
        return wi
    lines = []
    for key, label in [
        ("bank_name", "Bank Name"), ("bank_address", "Bank Address"),
        ("routing_number", "ABA/Routing"), ("swift_code", "SWIFT/BIC"),
        ("iban", "IBAN"), ("account_name", "Account Name"),
        ("account_number", "Account Number"), ("further_credit", "FFC"),
        ("reference", "Reference/Memo"),
    ]:
        if wi.get(key):
            lines.append(f"{label}: {wi[key]}")
    if wi.get("notes"):
        lines.append(f"\nNote: {wi['notes']}")
    return "\n".join(lines)
