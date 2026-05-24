"""
Entity-type-aware KYC document checklist generator.
Produces the required document list and the full KYC request email body
based on the investor's entity type.
"""

from typing import Optional

KYC_CHECKLISTS = {
    "Individual": [
        "Government-issued photo ID (passport or driver's license)",
        "Signed IRS Form W-9",
        "Proof of address (utility bill or bank statement dated within 90 days)",
        "Accredited investor verification (income/net worth documentation or third-party letter)",
    ],
    "LLC": [
        "Articles of Organization (state-filed)",
        "Operating Agreement (all pages + signature page)",
        "IRS EIN Determination Letter",
        "Certificate of Good Standing (dated within 6 months)",
        "Photo ID + signed W-9 for each beneficial owner with 25%+ interest",
        "Beneficial Ownership Certification form",
    ],
    "Trust": [
        "Full Trust Agreement (all pages including signature page)",
        "IRS EIN Determination Letter (or SSN if grantor trust)",
        "Certificate of Trust (if applicable)",
        "Photo ID + signed W-9 for each Trustee",
        "Accredited investor verification for the Trust",
    ],
    "LP": [
        "Certificate of Limited Partnership (state-filed)",
        "Limited Partnership Agreement (all pages + signature page)",
        "IRS EIN Determination Letter",
        "Certificate of Good Standing (dated within 6 months)",
        "Photo ID + signed W-9 for each General Partner",
        "Beneficial Ownership Certification form",
    ],
    "Corporation": [
        "Articles of Incorporation (state-filed)",
        "Corporate Bylaws",
        "IRS EIN Determination Letter",
        "Certificate of Good Standing (dated within 6 months)",
        "Corporate Resolution authorizing the investment",
        "Photo ID + signed W-9 for each authorized signatory",
        "Beneficial Ownership Certification form",
    ],
}

DEFAULT_CHECKLIST = [
    "Government-issued photo ID for all authorized signatories",
    "IRS EIN or SSN documentation",
    "Formation documents for the entity",
    "Proof of authorization to invest",
    "Signed W-9",
]


def get_checklist(entity_type: str) -> list[str]:
    return KYC_CHECKLISTS.get(entity_type, DEFAULT_CHECKLIST)


def build_kyc_email(
    entity_name: str,
    entity_type: str,
    offering_name: str,
    sharepoint_upload_link: str,
    advisor_name: Optional[str] = None,
    ops_contact_email: Optional[str] = None,
    deadline_days: int = 10,
) -> dict:
    """
    Build the KYC request email (Email 3).
    Returns a dict with subject and body ready to send via graph_client.
    """
    checklist = get_checklist(entity_type)
    checklist_text = "\n".join(f"  • {item}" for item in checklist)
    advisor_line = f"Your advisor {advisor_name} has initiated this process." if advisor_name else ""
    ops_line = f"Questions? Contact our operations team at {ops_contact_email}." if ops_contact_email else ""

    subject = f"Action Required: KYC Documentation for {offering_name}"

    body = f"""Dear {entity_name},

Thank you for your interest in {offering_name}. To proceed with your investment, our compliance team requires the following Know Your Customer (KYC) documentation for your {entity_type} entity.

{advisor_line}

REQUIRED DOCUMENTS FOR {entity_type.upper()}:
{checklist_text}

SECURE UPLOAD INSTRUCTIONS:
Please upload all documents to your secure, private folder using the link below. Only authorized parties have access.

  Upload Link: {sharepoint_upload_link}

All documents must be uploaded within {deadline_days} business days to hold your place in the fund.

IMPORTANT NOTES:
  • All documents should be clear, legible scans or photographs.
  • Expired identification documents will not be accepted.
  • If your entity has multiple beneficial owners or trustees, please include documentation for each.
  • If a document listed above does not apply to your entity, please include a brief note explaining why.

{ops_line}

We appreciate your prompt attention to this matter and look forward to welcoming you as an investor.

Warm regards,
Operations Team
"""

    return {"subject": subject, "body": body}


def build_kyc_followup_email(
    entity_name: str,
    offering_name: str,
    sharepoint_upload_link: str,
    days_outstanding: int,
) -> dict:
    """Build the KYC follow-up reminder email body."""
    subject = f"Reminder: KYC Documents Needed — {offering_name}"

    body = f"""Dear {entity_name},

We wanted to follow up regarding your pending KYC documentation for {offering_name}.

Our records show that your documents have been outstanding for {days_outstanding} business days. To ensure your commitment is processed and your position in the fund is secured, please upload your required documents at your earliest convenience.

  Upload Link: {sharepoint_upload_link}

If you have already submitted your documents or are experiencing any difficulty with the upload portal, please contact our operations team immediately.

Thank you,
Operations Team
"""

    return {"subject": subject, "body": body}
