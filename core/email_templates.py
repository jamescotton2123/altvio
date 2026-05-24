"""
Email template library for the full investor onboarding lifecycle.

Email 1 — DocuSign Dispatch Notice (sent immediately after onboarding)
Email 2 — Signed Documents + Wire Instructions (sent after DocuSign webhook fires)
Email 4 — Wire Follow-up Reminder (sent via follow-up scheduler after signing)

KYC emails (Email 3 + KYC follow-up) live in kyc_templates.py.

Firm overrides: rows in `email_templates` (see `api/routes/email_templates_crud.py`) replace
the default subject/body when `is_active` is true. Override `body_html` is returned as the
`body` field sent to Graph (same as built-in templates).
"""

from typing import Optional

from core.database import supabase


def _firm_override(firm_id: str | None, template_key: str) -> dict | None:
    """Return {subject, body_html} from email_templates if an active row exists, else None."""
    if not firm_id:
        return None
    try:
        result = (
            supabase.table("email_templates")
            .select("subject,body_html")
            .eq("firm_id", firm_id)
            .eq("template_key", template_key)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        return {"subject": rows[0]["subject"], "body_html": rows[0]["body_html"]}
    except Exception:
        return None


def _format_wire_breakdown_for_email(breakdown: Optional[dict]) -> str:
    """Build investor-facing text for commitment + third-party lines + carry disclosure."""
    if not breakdown:
        return ""
    lines = breakdown.get("lines") or []
    warnings = breakdown.get("warnings") or []
    carry = breakdown.get("carry_disclosures") or []
    total = breakdown.get("total_wire_due")
    parts: list[str] = [
        "FUNDING SUMMARY (wire the total below):",
        "",
    ]
    for li in lines:
        parts.append(f"  • {li.get('label', '')}: ${float(li.get('amount') or 0):,.2f}")
    parts.append("")
    parts.append(f"  Total wire due: ${float(total or 0):,.2f}")
    if warnings:
        parts.append("")
        parts.append("Notes:")
        for w in warnings:
            parts.append(f"  • {w}")
    if carry:
        parts.append("")
        parts.append("Carried interest (for your records — not added to wire total):")
        for c in carry:
            cp = c.get("carry_pct")
            ch = c.get("carry_hurdle_pct")
            parts.append(
                f"  • {c.get('party')}: carry {cp}% / hurdle {ch}% — does not change today's wire."
            )
    return "\n".join(parts)


def build_docusign_dispatch_email(
    entity_name: str,
    offering_name: str,
    committed_amount: float,
    advisor_name: Optional[str] = None,
    ops_contact_email: Optional[str] = None,
    firm_id: str | None = None,
) -> dict:
    """
    Email 1: Sent immediately after onboarding to notify the investor
    that their DocuSign subscription packet is on its way.
    """
    override = _firm_override(firm_id, "docusign_dispatch")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    formatted_amount = f"${committed_amount:,.2f}"
    advisor_line = (
        f"This investment has been initiated by your advisor, {advisor_name}."
        if advisor_name
        else ""
    )
    ops_line = (
        f"If you have any questions, please contact our operations team at {ops_contact_email}."
        if ops_contact_email
        else ""
    )

    subject = f"Your Subscription Documents for {offering_name} — Action Required"

    body = f"""Dear {entity_name},

We are pleased to confirm that your investment subscription for {offering_name} is now being processed.

COMMITMENT SUMMARY:
  • Fund: {offering_name}
  • Entity: {entity_name}
  • Commitment Amount: {formatted_amount}

{advisor_line}

NEXT STEPS:
You will receive a separate DocuSign email shortly containing your subscription agreement and advisory agreement (if applicable). Please review and execute all documents at your earliest convenience.

  • Review each document carefully before signing.
  • Ensure all information pre-filled in the documents is accurate.
  • If any information requires correction, do not sign — contact us immediately.

Wire instructions for funding your commitment will be provided upon completion of your signed documents.

{ops_line}

Thank you for your investment,
Operations Team
"""

    return {"subject": subject, "body": body}


def build_signed_docs_wire_email(
    entity_name: str,
    offering_name: str,
    committed_amount: float,
    sharepoint_link: str,
    wire_instructions: str = "",
    advisor_name: Optional[str] = None,
    ops_contact_email: Optional[str] = None,
    wire_delivery_mode: str = "inline",
    portal_url: Optional[str] = None,
    wire_breakdown: Optional[dict] = None,
    firm_id: str | None = None,
) -> dict:
    """
    Email 2: Sent after the DocuSign envelope-completed webhook fires.
    Delivers signed docs + wire instructions.

    wire_delivery_mode:
      'inline'      — wire instructions formatted as text in the email body
      'secure_link' — investors directed to their SharePoint folder (bank PDF)
      'portal'      — investors directed to their branded Altvio investor portal

    wire_breakdown: output of core.deal_fees.compute_commitment_wire_breakdown (commitment +
      third-party fees). If omitted, falls back to commitment-only totals.
    """
    override = _firm_override(firm_id, "signed_docs_wire")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    advisor_line = (
        f"Your advisor {advisor_name} has been copied on this communication."
        if advisor_name
        else ""
    )
    ops_line = (
        f"Questions? Reach our operations team at {ops_contact_email}."
        if ops_contact_email
        else ""
    )

    subject = f"{offering_name} — Signed Documents & Wire Instructions"

    if wire_breakdown is None:
        wire_breakdown = {
            "lines": [
                {"label": "Subscription commitment", "amount": float(committed_amount or 0)},
            ],
            "total_wire_due": float(committed_amount or 0),
            "warnings": [],
            "carry_disclosures": [],
        }

    if wire_delivery_mode == "portal" and portal_url:
        docs_section = f"""SIGNED DOCUMENTS & WIRE INSTRUCTIONS:
All your executed documents and wire instructions are available in your secure investor portal:

  {portal_url}

Your portal contains:
  • Executed Subscription Agreement
  • Executed Advisory Agreement (if applicable)
  • Official Wire Instructions (bank document)

Please reference your entity name "{entity_name}" in the wire memo field once you have reviewed the wire instructions."""

    elif wire_delivery_mode == "secure_link":
        docs_section = f"""SIGNED DOCUMENTS & WIRE INSTRUCTIONS:
Your executed documents and wire instructions are available in your secure document folder:

  {sharepoint_link}

Your folder contains:
  • Executed Subscription Agreement
  • Executed Advisory Agreement (if applicable)
  • Wire Instructions PDF (official bank document)

Please reference your entity name "{entity_name}" in the wire memo field."""

    else:
        docs_section = f"""SIGNED DOCUMENTS:
  Access your executed subscription packet here: {sharepoint_link}

WIRE INSTRUCTIONS:
Please reference your entity name in the wire memo field.

{wire_instructions}"""

    funding_block = _format_wire_breakdown_for_email(wire_breakdown)
    docs_section = f"{docs_section}\n\n{funding_block}"

    body = f"""Dear {entity_name},

Congratulations — your subscription documents for {offering_name} have been fully executed.

{docs_section}

{advisor_line}

IMPORTANT:
  • Wires typically take 1–3 business days to settle.
  • Please notify our operations team once your wire has been initiated.
  • Do not wire funds until you have reviewed your executed documents.
  • Your commitment is not considered funded until the wire is received and confirmed.

{ops_line}

We look forward to welcoming you as a valued investor in {offering_name}.

Warm regards,
Operations Team
"""

    return {"subject": subject, "body": body}


def build_wire_followup_email(
    entity_name: str,
    offering_name: str,
    committed_amount: float,
    wire_instructions: str,
    days_outstanding: int,
    ops_contact_email: Optional[str] = None,
    wire_breakdown: Optional[dict] = None,
    firm_id: str | None = None,
) -> dict:
    """
    Email 4: Wire funding follow-up reminder sent after documents are signed
    but wire has not yet been received.
    """
    override = _firm_override(firm_id, "wire_followup")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    if wire_breakdown is None:
        wire_breakdown = {
            "lines": [{"label": "Subscription commitment", "amount": float(committed_amount or 0)}],
            "total_wire_due": float(committed_amount or 0),
            "warnings": [],
            "carry_disclosures": [],
        }
    fb = _format_wire_breakdown_for_email(wire_breakdown)
    total_due = float(wire_breakdown.get("total_wire_due") or committed_amount or 0)
    formatted_total = f"${total_due:,.2f}"
    ops_line = (
        f"To confirm your wire or discuss any questions, contact {ops_contact_email}."
        if ops_contact_email
        else ""
    )

    subject = f"Reminder: Wire Instructions for {offering_name}"

    body = f"""Dear {entity_name},

We noticed that your wire for {offering_name} has not yet been received. Your subscription documents were signed {days_outstanding} business day(s) ago, and your total wire due of {formatted_total} is still pending.

{fb}

To secure your position in the fund, please initiate your wire at your earliest convenience using the instructions below:

{wire_instructions}

Please notify our operations team once your wire has been sent so we can confirm receipt promptly.

{ops_line}

Thank you,
Operations Team
"""

    return {"subject": subject, "body": body}


def build_funding_received_email(
    entity_name: str,
    offering_name: str,
    funded_amount: float,
    ops_contact_email: Optional[str] = None,
    firm_id: str | None = None,
) -> dict:
    """Sent to investor (cc advisor) when their wire is confirmed received."""
    override = _firm_override(firm_id, "funding_received")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    formatted = f"${funded_amount:,.2f}"
    ops_line = f"Questions? Contact {ops_contact_email}." if ops_contact_email else ""

    subject = f"{offering_name} — Wire Received, Commitment Confirmed"
    body = f"""Dear {entity_name},

We are pleased to confirm that your wire of {formatted} for {offering_name} has been received and your commitment is now fully funded.

COMMITMENT STATUS:
  • Fund: {offering_name}
  • Entity: {entity_name}
  • Amount Received: {formatted}
  • Status: Funded ✓

No further action is required at this time. You will receive distribution notices and fund updates as they become available.

{ops_line}

Thank you for your investment,
Operations Team
"""
    return {"subject": subject, "body": body}


def build_wire_early_email(
    entity_name: str,
    offering_name: str,
    committed_amount: float,
    wire_instructions: str,
    adv_pending: bool = True,
    ops_contact_email: Optional[str] = None,
    wire_breakdown: Optional[dict] = None,
    firm_id: str | None = None,
) -> dict:
    """Sent when ops sends wire instructions before the advisory agreement is signed."""
    override = _firm_override(firm_id, "wire_early")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    if wire_breakdown is None:
        wire_breakdown = {
            "lines": [{"label": "Subscription commitment", "amount": float(committed_amount or 0)}],
            "total_wire_due": float(committed_amount or 0),
            "warnings": [],
            "carry_disclosures": [],
        }
    fb = _format_wire_breakdown_for_email(wire_breakdown)
    total_due = float(wire_breakdown.get("total_wire_due") or committed_amount or 0)
    formatted_total = f"${total_due:,.2f}"
    adv_notice = (
        "\nPLEASE NOTE: Your Advisory Agreement is still awaiting your signature. "
        "Please sign it at your earliest convenience — we will follow up separately.\n"
        if adv_pending else ""
    )
    ops_line = f"Questions? Contact {ops_contact_email}." if ops_contact_email else ""

    subject = f"{offering_name} — Wire Instructions"
    body = f"""Dear {entity_name},

Please find the wire instructions below to fund your total wire due of {formatted_total} to {offering_name}.
{adv_notice}
{fb}

WIRE INSTRUCTIONS:
{wire_instructions}

Please reference your entity name in the wire memo field and notify our operations team once your wire has been initiated.

{ops_line}

Thank you,
Operations Team
"""
    return {"subject": subject, "body": body}


def build_wire_missing_request_email(
    entity_name: str,
    offering_name: str,
    ops_contact_email: Optional[str] = None,
    firm_id: str | None = None,
) -> dict:
    """Sent to investor when wire instructions are missing from their record."""
    override = _firm_override(firm_id, "wire_missing")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    ops_line = f"Reply to this email or contact {ops_contact_email}." if ops_contact_email else "Please reply to this email."

    subject = f"Action Required: Wire Instructions Needed — {offering_name}"
    body = f"""Dear {entity_name},

We do not have wire instructions on file for your account in connection with {offering_name}.

To process future distributions and transactions, please provide your banking details by replying to this email or contacting our operations team.

You will need to provide:
  • Bank Name
  • ABA / Routing Number
  • Account Number
  • Account Name (as it appears on the account)
  • Account Type (Checking / Savings)
  • Any special reference or memo instructions

{ops_line}

Thank you,
Operations Team
"""
    return {"subject": subject, "body": body}


def build_negative_consent_email(
    entity_name: str,
    offering_name: str,
    distribution_amount: float,
    distribution_date: str,
    opt_out_deadline: str,
    ops_contact_email: Optional[str] = None,
    firm_id: str | None = None,
) -> dict:
    """Negative consent notice sent before initiating a distribution."""
    override = _firm_override(firm_id, "negative_consent")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    formatted = f"${distribution_amount:,.2f}"
    ops_line = f"Contact {ops_contact_email} to opt out or update wire instructions." if ops_contact_email else "Please reply to this email to opt out or update your wire instructions."

    subject = f"{offering_name} — Upcoming Distribution Notice"
    body = f"""Dear {entity_name},

We are writing to notify you of an upcoming distribution from {offering_name}.

DISTRIBUTION DETAILS:
  • Fund: {offering_name}
  • Distribution Amount: {formatted}
  • Planned Distribution Date: {distribution_date}
  • Payment Method: Wire transfer to banking information currently on file

No action is required if you wish to receive this distribution at your wire instructions on file.

If you wish to opt out of this distribution or update your wire instructions, you must notify us by {opt_out_deadline}.

{ops_line}

Thank you,
Operations Team
"""
    return {"subject": subject, "body": body}


def build_distribution_notice_email(
    entity_name: str,
    offering_name: str,
    individual_amount: float,
    distribution_date: str,
    distribution_type: str,
    payment_method: str = "Wire Transfer",
    ops_contact_email: Optional[str] = None,
    firm_id: str | None = None,
) -> dict:
    """
    Distribution notice email with individualized calculation.
    """
    override = _firm_override(firm_id, "distribution_notice")
    if override:
        return {"subject": override["subject"], "body": override["body_html"]}

    formatted_amount = f"${individual_amount:,.2f}"
    ops_line = (
        f"For questions regarding this distribution, contact {ops_contact_email}."
        if ops_contact_email
        else ""
    )

    subject = f"{offering_name} — {distribution_type.title()} Distribution Notice"

    body = f"""Dear {entity_name},

We are pleased to inform you that a {distribution_type} distribution has been declared for {offering_name}.

DISTRIBUTION DETAILS:
  • Fund: {offering_name}
  • Distribution Type: {distribution_type.title()}
  • Distribution Date: {distribution_date}
  • Your Distribution Amount: {formatted_amount}
  • Payment Method: {payment_method}

Your distribution will be processed using the banking information on file. Please allow 3–5 business days for the funds to appear in your account.

If your banking information has changed or you have any questions regarding this distribution, please contact our operations team immediately.

{ops_line}

Thank you for your continued partnership.

Warm regards,
Operations Team
"""

    return {"subject": subject, "body": body}
