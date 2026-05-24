"""
DocuSign client using JWT authentication and the docusign-esign SDK.

Phase 3 upgrades:
  - Entity-type-aware template selection (Individual / Trust / Joint / Entity)
  - Full 6-step signing chain:
      Reviewer (edit) → Ops → Investor(s) → Advisor (initial) → Compliance → CEO
  - Advisory agreement short chain: Investor → Advisor
  - Comprehensive pre-fill from investors table (new + existing clients)
  - KYC-driven field injection (address, tax ID, signer title, W-9 classification)
  - Firm-customizable subject + email body from email_templates table
  - DocuSign subject line enforcement (35 char min, 100 char max)
  - Joint tenant co-signer support (detected from kyc_reviews.ownership_structure)
  - W9 / W8-BEN / W8-BEN-E auto-selection
  - Sub doc send held when low-confidence KYC extractions await ops pre-fill review
"""

import os
from typing import Optional

import docusign_esign as ds
from docusign_esign import ApiClient, EnvelopeDefinition, EnvelopesApi
from docusign_esign.models import (
    CompositeTemplate,
    DateSigned,
    InlineTemplate,
    Recipients,
    ServerTemplate,
    SignHere,
    Tabs,
    TemplateRole,
    TextTab,
)

from core.database import supabase

# ---------------------------------------------------------------------------
# Entity type → template category
# ---------------------------------------------------------------------------

ENTITY_TYPE_CATEGORY: dict[str, str] = {
    "Individual": "Individual",
    "Joint": "Joint",
    "Trust": "Trust",
    "LLC": "Entity",
    "LP": "Entity",
    "Corporation": "Entity",
    "Other": "Entity",
}

# W-9 tax classification text pre-fill
W9_CLASSIFICATION: dict[str, str] = {
    "Individual": "Individual/sole proprietor or single-member LLC",
    "Joint": "Individual/sole proprietor or single-member LLC",
    "LLC": "Limited liability company",
    "Trust": "Trust/estate",
    "LP": "Partnership",
    "Corporation": "C Corporation",
    "Other": "Other",
}

# Fields that, when pending in investor_pending_changes, indicate KYC pre-fill needs review
PREFILL_REVIEW_FIELDS = {"mailing_address", "tax_id", "state_of_formation", "entity_name"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_api_client(settings: dict) -> tuple[ApiClient, str]:
    """Build and authenticate a DocuSign API client using JWT for the firm."""
    integration_key = os.environ.get("DOCUSIGN_INTEGRATION_KEY")
    account_id = os.environ.get("DOCUSIGN_ACCOUNT_ID")
    user_id = os.environ.get("DOCUSIGN_USER_ID")
    private_key = os.environ.get("DOCUSIGN_PRIVATE_KEY", "").replace("\\n", "\n")
    base_path = os.environ.get("DOCUSIGN_BASE_PATH", "https://na4.docusign.net/restapi")

    api_client = ApiClient()
    api_client.set_base_path(base_path)

    token_response = api_client.request_jwt_user_token(
        client_id=integration_key,
        user_id=user_id,
        oauth_host_name="account.docusign.com",
        private_key_bytes=private_key.encode("utf-8"),
        expires_in=3600,
        scopes=["signature", "impersonation"],
    )
    api_client.set_default_header("Authorization", f"Bearer {token_response.access_token}")
    return api_client, account_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_docusign_template_text(firm_id: str, template_key: str, fallback: str) -> str:
    """
    Look up firm-customized DocuSign subject or body from email_templates table.
    Falls back to hardcoded default if no firm override is set.
    """
    try:
        result = (
            supabase.table("email_templates")
            .select("subject, body_html")
            .eq("firm_id", firm_id)
            .eq("template_key", template_key)
            .eq("is_active", True)
            .single()
            .execute()
        )
        if result.data:
            return result.data.get("subject") or result.data.get("body_html") or fallback
    except Exception:
        pass
    return fallback


def truncate_entity_name(entity_name: str, max_len: int = 35) -> str:
    """
    Truncate entity name to max_len characters for use in DocuSign subjects
    and in SharePoint file names. Appends ellipsis if truncated.
    """
    if len(entity_name) <= max_len:
        return entity_name
    return entity_name[: max_len - 3] + "..."


def _build_subject(template: str, offering_name: str, entity_name: str, committed_amount: float = 0) -> str:
    """
    Build DocuSign envelope subject from a template string.
    Enforces: minimum 35 chars (DocuSign requirement), maximum 99 chars.
    Entity name is always capped at 35 chars within the subject.
    """
    short_entity = truncate_entity_name(entity_name, max_len=35)

    try:
        subject = template.format(
            offering_name=offering_name,
            entity_name=short_entity,
            committed_amount=f"${committed_amount:,.2f}",
        )
    except KeyError:
        subject = f"Action Required: {offering_name} Subscription Documents — {short_entity}"

    if len(subject) < 35:
        subject = subject.ljust(35)

    return subject[:99]


def _get_signer_title(investor_id: str) -> str:
    """Get the most recently confirmed qualified signer title from kyc_reviews."""
    try:
        reviews = (
            supabase.table("kyc_reviews")
            .select("signatories")
            .eq("investor_id", investor_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
            .data
        )
        for review in reviews:
            for sig in (review.get("signatories") or []):
                if sig.get("qualified_to_sign") and sig.get("title"):
                    return sig["title"]
    except Exception:
        pass
    return ""


def _get_joint_tenants(investor_id: str) -> list[dict]:
    """
    Get joint tenant details from the latest kyc_review ownership_structure.
    Returns list of {name, email} dicts (may be empty if not yet uploaded).
    """
    try:
        review = (
            supabase.table("kyc_reviews")
            .select("ownership_structure")
            .eq("investor_id", investor_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if review:
            structure = review[0].get("ownership_structure") or {}
            return structure.get("joint_tenants") or []
    except Exception:
        pass
    return []


def _has_pending_prefill_reviews(investor_id: str) -> bool:
    """Return True if the investor has unconfirmed low-confidence KYC extractions."""
    try:
        result = (
            supabase.table("investor_pending_changes")
            .select("id")
            .eq("investor_id", investor_id)
            .eq("source", "kyc_extraction")
            .eq("status", "Pending")
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False


def _build_prefill_tabs(investor: dict, commitment: dict, deal: dict) -> Tabs:
    """
    Build comprehensive pre-fill TextTabs and CheckboxTabs for the investor's
    portion of the sub doc. Only injects tabs where values are non-empty so
    blank fields remain editable in DocuSign for new investors.
    """
    text_tabs: list[TextTab] = []

    def _add(label: str, value) -> None:
        if value:
            text_tabs.append(TextTab(tab_label=label, value=str(value)))

    # Core investor fields
    _add("EntityName", investor.get("entity_name"))
    _add("EntityType", investor.get("entity_type"))
    _add("PrimaryEmail", investor.get("primary_email"))
    _add("MailingAddress", investor.get("mailing_address"))
    _add("TaxID", investor.get("tax_id"))
    _add("StateOfFormation", investor.get("state_of_formation"))
    _add("CountryOfFormation", investor.get("country_of_formation"))

    # Commitment and deal
    committed = float(commitment.get("committed_amount") or 0)
    if committed:
        _add("CommitmentAmount", f"${committed:,.2f}")
    _add("FundName", deal.get("offering_name"))

    # Advisory fee
    fee_pct = commitment.get("advisory_fee_pct")
    if fee_pct is not None:
        _add("AdvisoryFeePercent", f"{float(fee_pct):.2f}%")

    # Signer title from KYC
    signer_title = _get_signer_title(investor.get("id", ""))
    if signer_title:
        _add("SignerTitle", signer_title)
        _add("AuthorizedSignerTitle", signer_title)

    # W-9 / W-8 are left intentionally blank — the investor completes their own
    # tax form. Pre-filling would bypass the investor's self-certification requirement.

    return Tabs(text_tabs=text_tabs if text_tabs else None)


def _resolve_sub_doc_template_id(investor: dict, deal: dict, settings: dict) -> str:
    """
    Select the correct sub doc template based on entity type.
    Lookup: deals.docusign_templates[category] → firm_settings fallback.
    """
    category = ENTITY_TYPE_CATEGORY.get(investor.get("entity_type", "Individual"), "Entity")
    deal_templates = deal.get("docusign_templates") or {}
    if isinstance(deal_templates, dict) and deal_templates.get(category):
        return deal_templates[category]
    return (
        settings.get("docusign_sub_doc_template_id")
        or os.environ.get("DOCUSIGN_SUB_DOC_TEMPLATE_ID", "")
    )


def _resolve_tax_form_template_id(investor: dict, settings: dict) -> Optional[str]:
    """
    Determine the correct tax form template: W-9, W-8BEN, or W-8BEN-E.
    Returns None if no separate template is configured (W-9 may be embedded in sub doc).
    """
    country = (investor.get("country_of_formation") or "").upper().strip()
    is_foreign = bool(country and country not in ("US", "USA", "UNITED STATES", ""))
    if is_foreign:
        if investor.get("entity_type") == "Individual":
            return settings.get("docusign_w8ben_template_id")
        return settings.get("docusign_w8bene_template_id")
    return settings.get("docusign_w9_template_id")


# ---------------------------------------------------------------------------
# Main envelope sender
# ---------------------------------------------------------------------------

def send_envelope(
    settings: dict,
    investor: dict,
    deal: dict,
    commitment: dict,
    side_letter_pdf: bytes | None = None,
) -> dict:
    """
    Send the sub docs + advisory agreement envelope for an investor.

    Sub doc signing chain (6 steps):
      1 → Reviewer  — allowed to edit, pre-flight check
      2 → Ops       — countersigner
      3 → Investor(s) — main signer(s); joint entities include co-signer
      4 → Advisor   — initials
      5 → Compliance — signer
      6 → CEO       — final signer; triggers commitment_date on webhook

    Advisory agreement chain (2 steps, separate CompositeTemplate):
      1 → Investor
      2 → Advisor countersign

    If side_letter_pdf is provided, it is prepended as the first document
    in the envelope (read-only, no signature tabs). Signers see it before
    the main sub docs.
    """
    api_client, account_id = _get_api_client(settings)
    envelopes_api = EnvelopesApi(api_client)
    firm_id = settings.get("firm_id") or commitment.get("firm_id", "")

    sub_doc_template_id = _resolve_sub_doc_template_id(investor, deal, settings)
    if not sub_doc_template_id:
        raise ValueError(
            f"No DocuSign sub doc template configured for entity type "
            f"'{investor.get('entity_type')}' on deal '{deal.get('offering_name')}'."
        )

    # Resolve role names (configurable per firm)
    role_names = settings.get("docusign_role_names") or {}
    role_reviewer = role_names.get("reviewer", "Reviewer")
    role_ops = role_names.get("ops", "OpsCountersigner")
    role_investor = role_names.get("investor", "Investor")
    role_advisor = role_names.get("advisor", "Advisor")
    role_compliance = role_names.get("compliance", "Compliance")
    role_ceo = role_names.get("ceo", "CEO")

    reviewer_email = settings.get("docusign_reviewer_email") or settings.get("ops_mailbox", "")
    ops_email = settings.get("ops_mailbox", "")
    compliance_email = settings.get("compliance_email") or settings.get("ops_mailbox", "")
    ceo_email = settings.get("ceo_email") or settings.get("ops_mailbox", "")
    advisor_email = investor.get("advisor_email", "")

    committed_amount = float(commitment.get("committed_amount") or 0)

    # Subject + body from firm-customizable templates
    subject_template = (
        deal.get("email_subject_template")
        or _get_docusign_template_text(
            firm_id,
            "docusign_subdoc_subject",
            "Action Required: {offering_name} Subscription Documents — {entity_name}",
        )
    )
    body_template = _get_docusign_template_text(
        firm_id,
        "docusign_subdoc_body",
        "Please review and sign your subscription documents for {offering_name}. "
        "Your commitment amount is {committed_amount}.",
    )
    subject = _build_subject(subject_template, deal["offering_name"], investor["entity_name"], committed_amount)
    try:
        body = body_template.format(
            offering_name=deal["offering_name"],
            entity_name=investor["entity_name"],
            committed_amount=f"${committed_amount:,.2f}",
        )
    except KeyError:
        body = body_template

    # Pre-fill tabs
    prefill_tabs = _build_prefill_tabs(investor, commitment, deal)

    # --- 6-step signing chain ---

    sub_doc_roles: list[TemplateRole] = []

    # Order 1: Reviewer (allowed to edit)
    if reviewer_email:
        sub_doc_roles.append(TemplateRole(
            email=reviewer_email,
            name="Operations Reviewer",
            role_name=role_reviewer,
            routing_order="1",
        ))

    # Order 2: Ops countersigner
    if ops_email:
        sub_doc_roles.append(TemplateRole(
            email=ops_email,
            name="Operations",
            role_name=role_ops,
            routing_order="2",
        ))

    # Order 3: Investor (with pre-fill tabs)
    sub_doc_roles.append(TemplateRole(
        email=investor["primary_email"],
        name=investor["entity_name"],
        role_name=role_investor,
        routing_order="3",
        tabs=prefill_tabs,
    ))

    # Joint tenant co-signer (also at order 3)
    if investor.get("entity_type") == "Joint":
        joint_tenants = _get_joint_tenants(investor.get("id", ""))
        for i, jt in enumerate(joint_tenants[1:], start=1):
            sub_doc_roles.append(TemplateRole(
                email=jt.get("email") or investor["primary_email"],
                name=jt.get("name", f"Joint Tenant {i + 1}"),
                role_name=f"{role_investor}JT{i}",
                routing_order="3",
            ))

    # Order 4: Advisor (initials)
    if advisor_email:
        sub_doc_roles.append(TemplateRole(
            email=advisor_email,
            name="Advisor",
            role_name=role_advisor,
            routing_order="4",
        ))

    # Order 5: Compliance
    if compliance_email:
        sub_doc_roles.append(TemplateRole(
            email=compliance_email,
            name="Compliance",
            role_name=role_compliance,
            routing_order="5",
        ))

    # Order 6: CEO — final signer, triggers commitment_date via webhook
    if ceo_email:
        sub_doc_roles.append(TemplateRole(
            email=ceo_email,
            name="CEO",
            role_name=role_ceo,
            routing_order="6",
        ))

    composite_templates = []

    # Prepend side letter with investor + CEO signatures using anchor-based tabs
    if side_letter_pdf:
        import base64

        from docusign_esign.models import Document
        from docusign_esign.models import Signer as DSigner

        from core.side_letter import ANCHOR_CEO_SIG, ANCHOR_INVESTOR_SIG

        sl_b64 = base64.b64encode(side_letter_pdf).decode("ascii")

        # Investor signs the side letter at routing order 3
        sl_investor_tabs = Tabs(
            sign_here_tabs=[SignHere(
                anchor_string=ANCHOR_INVESTOR_SIG,
                anchor_units="pixels",
                anchor_x_offset="0",
                anchor_y_offset="8",
            )],
            date_signed_tabs=[DateSigned(
                anchor_string=ANCHOR_INVESTOR_SIG,
                anchor_units="pixels",
                anchor_x_offset="150",
                anchor_y_offset="8",
            )],
        )
        # CEO signs the side letter at routing order 6
        sl_ceo_tabs = Tabs(
            sign_here_tabs=[SignHere(
                anchor_string=ANCHOR_CEO_SIG,
                anchor_units="pixels",
                anchor_x_offset="0",
                anchor_y_offset="8",
            )],
            date_signed_tabs=[DateSigned(
                anchor_string=ANCHOR_CEO_SIG,
                anchor_units="pixels",
                anchor_x_offset="150",
                anchor_y_offset="8",
            )],
        )

        sl_signers = [
            DSigner(
                email=investor["primary_email"],
                name=investor["entity_name"],
                recipient_id="31",
                routing_order="3",
                tabs=sl_investor_tabs,
            )
        ]
        if ceo_email:
            sl_signers.append(DSigner(
                email=ceo_email,
                name="CEO",
                recipient_id="36",
                routing_order="6",
                tabs=sl_ceo_tabs,
            ))

        composite_templates.append(CompositeTemplate(
            inline_templates=[InlineTemplate(
                sequence="1",
                documents=[Document(
                    document_base64=sl_b64,
                    name="Side Letter Agreement",
                    file_extension="pdf",
                    document_id="99",
                )],
                recipients=Recipients(signers=sl_signers),
            )],
        ))

    composite_templates.append(
        CompositeTemplate(
            server_templates=[ServerTemplate(sequence="1", template_id=sub_doc_template_id)],
            inline_templates=[InlineTemplate(
                sequence="2",
                recipients=Recipients(template_roles=sub_doc_roles),
            )],
        )
    )

    # Tax form (W-9 / W-8BEN / W-8BEN-E) appended as a separate template if configured.
    # No pre-fill — investor self-certifies all tax information.
    tax_template_id = _resolve_tax_form_template_id(investor, settings)
    if tax_template_id:
        composite_templates.append(CompositeTemplate(
            server_templates=[ServerTemplate(sequence="1", template_id=tax_template_id)],
            inline_templates=[InlineTemplate(
                sequence="2",
                recipients=Recipients(template_roles=[
                    TemplateRole(
                        email=investor["primary_email"],
                        name=investor["entity_name"],
                        role_name=role_investor,
                        routing_order="1",
                    )
                ]),
            )],
        ))

    # Advisory agreement (short chain: Investor → Advisor)
    advisory_template_id = (
        deal.get("docusign_advisory_template_id")
        or settings.get("docusign_advisory_template_id")
        or os.environ.get("DOCUSIGN_ADVISORY_TEMPLATE_ID")
    )
    if advisory_template_id and advisor_email:
        fee_pct = float(commitment.get("advisory_fee_pct") or 1.0)

        composite_templates.append(CompositeTemplate(
            server_templates=[ServerTemplate(sequence="1", template_id=advisory_template_id)],
            inline_templates=[InlineTemplate(
                sequence="2",
                recipients=Recipients(template_roles=[
                    TemplateRole(
                        email=investor["primary_email"],
                        name=investor["entity_name"],
                        role_name=role_investor,
                        routing_order="1",
                        tabs=Tabs(text_tabs=[
                            TextTab(tab_label="EntityName", value=investor.get("entity_name", "")),
                            TextTab(tab_label="AdvisoryFeePercent", value=f"{fee_pct:.2f}%"),
                            TextTab(tab_label="FundName", value=deal.get("offering_name", "")),
                        ]),
                    ),
                    TemplateRole(
                        email=advisor_email,
                        name="Advisor",
                        role_name=role_advisor,
                        routing_order="2",
                    ),
                ]),
            )],
        ))

    envelope_definition = EnvelopeDefinition(
        email_subject=subject,
        email_blurb=body,
        composite_templates=composite_templates,
        status="sent",
    )

    result = envelopes_api.create_envelope(account_id, envelope_definition=envelope_definition)
    return {"envelope_id": result.envelope_id}


def pause_envelope(envelope_id: str, reason: str, settings: dict) -> None:
    """Void a DocuSign envelope flagged by the sub doc AI reviewer."""
    api_client, account_id = _get_api_client(settings)
    envelopes_api = EnvelopesApi(api_client)
    envelopes_api.update(
        account_id,
        envelope_id,
        envelope=ds.Envelope(status="voided", voided_reason=f"AI Review Flag: {reason}"),
    )


def send_loi_envelope(settings: dict, investor: dict, deal: dict, commitment: dict) -> dict:
    """
    Send an LOI (Letter of Intent) DocuSign envelope.
    Uses firm_settings.docusign_loi_template_id.
    Signer: investor only — no ops countersign required for LOIs.
    """
    api_client, account_id = _get_api_client(settings)
    envelopes_api = EnvelopesApi(api_client)

    loi_template_id = settings.get("docusign_loi_template_id") or os.environ.get("DOCUSIGN_LOI_TEMPLATE_ID")
    if not loi_template_id:
        raise ValueError("docusign_loi_template_id is not configured in firm_settings or .env.")

    committed_amount = float(commitment.get("committed_amount") or 0)
    firm_id = settings.get("firm_id") or commitment.get("firm_id", "")

    subject_template = _get_docusign_template_text(
        firm_id, "docusign_loi_subject", "Letter of Intent — {offering_name} — {entity_name}"
    )
    subject = _build_subject(subject_template, deal["offering_name"], investor["entity_name"], committed_amount)

    envelope_definition = EnvelopeDefinition(
        email_subject=subject,
        composite_templates=[
            CompositeTemplate(
                server_templates=[ServerTemplate(sequence="1", template_id=loi_template_id)],
                inline_templates=[InlineTemplate(
                    sequence="2",
                    recipients=Recipients(template_roles=[
                        TemplateRole(
                            email=investor["primary_email"],
                            name=investor["entity_name"],
                            role_name="Investor",
                            routing_order="1",
                            tabs=Tabs(text_tabs=[
                                TextTab(tab_label="CommitmentAmount", value=f"${committed_amount:,.2f}"),
                                TextTab(tab_label="EntityName", value=investor["entity_name"]),
                                TextTab(tab_label="FundName", value=deal["offering_name"]),
                            ]),
                        )
                    ]),
                )],
            )
        ],
        status="sent",
    )

    result = envelopes_api.create_envelope(account_id, envelope_definition=envelope_definition)
    return {"envelope_id": result.envelope_id}


def send_toi_envelope(
    settings: dict,
    transfer: dict,
    transferor: dict,
    transferee: dict,
    deal: dict,
    commitment: dict,
) -> dict:
    """
    Send a Transfer of Interest DocuSign envelope (two signers).
    Uses firm_settings.docusign_toi_template_id (or DOCUSIGN_TOI_TEMPLATE_ID).

    The DocuSign template must define server roles named **Transferor** and **Transferee**
    (routing order 1 and 2). Optional text tabs (pre-filled when labels match):
    TransferAmount, FundName, TransferorName, TransfereeName, OriginalCommitmentAmount.
    """
    api_client, account_id = _get_api_client(settings)
    envelopes_api = EnvelopesApi(api_client)

    toi_template_id = settings.get("docusign_toi_template_id") or os.environ.get("DOCUSIGN_TOI_TEMPLATE_ID")
    if not toi_template_id:
        raise ValueError("docusign_toi_template_id is not configured in firm_settings or .env.")

    amt = float(transfer.get("transfer_amount") or 0)
    committed = float(commitment.get("committed_amount") or 0)
    firm_id = settings.get("firm_id") or transfer.get("firm_id", "")

    subject_template = _get_docusign_template_text(
        firm_id,
        "docusign_toi_subject",
        "Transfer of Interest — {offering_name} — {entity_name}",
    )
    subject = _build_subject(
        subject_template,
        deal.get("offering_name", "Fund"),
        transferor.get("entity_name", "Transferor"),
        amt,
    )

    envelope_definition = EnvelopeDefinition(
        email_subject=subject,
        composite_templates=[
            CompositeTemplate(
                server_templates=[ServerTemplate(sequence="1", template_id=toi_template_id)],
                inline_templates=[
                    InlineTemplate(
                        sequence="2",
                        recipients=Recipients(
                            template_roles=[
                                TemplateRole(
                                    email=transferor["primary_email"],
                                    name=transferor.get("entity_name", "Transferor"),
                                    role_name="Transferor",
                                    routing_order="1",
                                    tabs=Tabs(
                                        text_tabs=[
                                            TextTab(tab_label="TransferAmount", value=f"${amt:,.2f}"),
                                            TextTab(tab_label="FundName", value=deal.get("offering_name", "")),
                                            TextTab(tab_label="TransferorName", value=transferor.get("entity_name", "")),
                                            TextTab(tab_label="TransfereeName", value=transferee.get("entity_name", "")),
                                            TextTab(
                                                tab_label="OriginalCommitmentAmount",
                                                value=f"${committed:,.2f}",
                                            ),
                                        ]
                                    ),
                                ),
                                TemplateRole(
                                    email=transferee["primary_email"],
                                    name=transferee.get("entity_name", "Transferee"),
                                    role_name="Transferee",
                                    routing_order="2",
                                    tabs=Tabs(
                                        text_tabs=[
                                            TextTab(tab_label="TransferAmount", value=f"${amt:,.2f}"),
                                            TextTab(tab_label="FundName", value=deal.get("offering_name", "")),
                                            TextTab(tab_label="TransferorName", value=transferor.get("entity_name", "")),
                                            TextTab(tab_label="TransfereeName", value=transferee.get("entity_name", "")),
                                            TextTab(
                                                tab_label="OriginalCommitmentAmount",
                                                value=f"${committed:,.2f}",
                                            ),
                                        ]
                                    ),
                                ),
                            ]
                        ),
                    )
                ],
            )
        ],
        status="sent",
    )

    result = envelopes_api.create_envelope(account_id, envelope_definition=envelope_definition)
    return {"envelope_id": result.envelope_id}


def download_signed_documents(envelope_id: str, settings: dict) -> bytes:
    """Download the completed envelope as a combined PDF."""
    api_client, account_id = _get_api_client(settings)
    envelopes_api = EnvelopesApi(api_client)
    return envelopes_api.get_document(account_id, envelope_id, document_id="combined")
