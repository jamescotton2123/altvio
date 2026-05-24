"""
Distribution Bot.
Two modes:

1. Individualized Distribution Notices
   - Calculates each investor's pro-rata share
   - Sends personalized emails with their specific amount
   - Logs to distribution_notices table

2. BCC Blast (audited financials or general notices)
   - Grabs a PDF from SharePoint
   - Puts all fund investors on BCC
   - Sends one email with the attachment
"""

import logging
from datetime import date

from core.database import supabase

logger = logging.getLogger(__name__)


def calculate_pro_rata(committed_amount: float, total_committed: float, total_distribution: float) -> float:
    """Calculate an investor's pro-rata distribution share."""
    if total_committed == 0:
        return 0.0
    return round((committed_amount / total_committed) * total_distribution, 2)


def send_distribution_notices(
    firm_id: str,
    deal_id: str,
    total_distribution_amount: float,
    distribution_type: str,
    distribution_date: str,
    settings: dict,
    notes: str = "",
) -> dict:
    """
    Send individualized distribution notices to all funded investors in a deal.
    Calculates pro-rata for each investor and sends a personalized email.
    Writes to distributions and distribution_notices tables.
    """
    from core.email_templates import build_distribution_notice_email
    from core.graph_client import send_email

    ops_mailbox = settings.get("ops_mailbox")

    # Create the distribution record
    dist_resp = supabase.table("distributions").insert({
        "firm_id": firm_id,
        "deal_id": deal_id,
        "distribution_date": distribution_date,
        "total_amount": total_distribution_amount,
        "distribution_type": distribution_type,
        "notes": notes,
    }).execute()
    distribution_id = dist_resp.data[0]["id"]

    # Fetch all funded commitments for this deal
    commitments = (
        supabase.table("commitments")
        .select("id, committed_amount, funded_amount, investor_id, investors(entity_name, primary_email, advisor_email), deals(offering_name)")
        .eq("firm_id", firm_id)
        .eq("deal_id", deal_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )

    if not commitments:
        logger.warning("No funded investors found for deal %s.", deal_id)
        return {"distribution_id": distribution_id, "sent_count": 0}

    # Calculate total committed for pro-rata denominator
    total_committed = sum(c["committed_amount"] for c in commitments)
    sent_count = 0

    for commitment in commitments:
        investor = commitment.get("investors", {})
        deal = commitment.get("deals", {})
        investor_email = investor.get("primary_email")
        if not investor_email:
            continue

        individual_amount = calculate_pro_rata(
            committed_amount=commitment["committed_amount"],
            total_committed=total_committed,
            total_distribution=total_distribution_amount,
        )

        email = build_distribution_notice_email(
            entity_name=investor["entity_name"],
            offering_name=deal.get("offering_name", ""),
            individual_amount=individual_amount,
            distribution_date=distribution_date,
            distribution_type=distribution_type,
            ops_contact_email=ops_mailbox,
            firm_id=firm_id,
        )

        send_email(
            settings=settings,
            to=investor_email,
            cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
            subject=email["subject"],
            body=email["body"],
        )

        # Log the notice
        supabase.table("distribution_notices").insert({
            "firm_id": firm_id,
            "distribution_id": distribution_id,
            "investor_id": commitment["investor_id"],
            "individual_amount": individual_amount,
            "sent_at": date.today().isoformat(),
            "status": "Sent",
        }).execute()

        sent_count += 1
        logger.info("Distribution notice sent to %s for $%s.", investor["entity_name"], f"{individual_amount:,.2f}")

    logger.info("Distribution complete. %s notices sent for deal %s.", sent_count, deal_id)
    return {"distribution_id": distribution_id, "sent_count": sent_count}


def send_bcc_blast(
    firm_id: str,
    deal_id: str,
    subject: str,
    body: str,
    settings: dict,
    pdf_sharepoint_path: str = None,
    pdf_bytes: bytes = None,
    pdf_filename: str = "document.pdf",
) -> dict:
    """
    Send a single email to all investors in a fund via BCC.
    Used for audited financials, K-1s, general fund announcements.
    PDF can be provided as raw bytes or fetched from SharePoint via path.

    All investor emails are BCC'd — no investor sees the other recipients.
    """
    import requests

    from core.http_retry import REQUEST_TIMEOUT_SECONDS, request_with_retry

    ops_mailbox = settings.get("ops_mailbox")

    # Fetch all funded investor emails for this deal
    commitments = (
        supabase.table("commitments")
        .select("investors(entity_name, primary_email)")
        .eq("firm_id", firm_id)
        .eq("deal_id", deal_id)
        .eq("wire_status", "Funded")
        .eq("status", "Active")
        .execute()
        .data
    )

    bcc_emails = [
        c["investors"]["primary_email"]
        for c in commitments
        if c.get("investors", {}).get("primary_email")
    ]

    if not bcc_emails:
        logger.warning("No funded investor emails found for deal %s.", deal_id)
        return {"sent_count": 0, "bcc_count": 0}

    attachments = []
    if pdf_bytes:
        attachments = [{"name": pdf_filename, "content_bytes": pdf_bytes}]

    # Send to ops mailbox as the "To" field, all investors on BCC

    from core.graph_client import _get_access_token

    token = _get_access_token(settings)
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": ops_mailbox}}],
        "bccRecipients": [{"emailAddress": {"address": email}} for email in bcc_emails],
    }
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["name"],
                "contentBytes": __import__("base64").b64encode(att["content_bytes"]).decode(),
            }
            for att in attachments
        ]

    send_url = f"https://graph.microsoft.com/v1.0/users/{ops_mailbox}/sendMail"
    resp = request_with_retry(
        requests.post,
        send_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    logger.info("BCC blast sent to %s investors for deal %s.", len(bcc_emails), deal_id)
    return {"sent_count": 1, "bcc_count": len(bcc_emails)}
