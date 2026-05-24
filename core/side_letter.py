"""
Side letter drafter.

Each firm has a template stored in firm_settings.side_letter_template.
The template is a plain-text (or lightly HTML) document with {{ placeholders }}
for investor-specific fields and provision language.

GPT fills the placeholders and returns a finished draft.
Ops reviews it via GET /commitments/{id}/side-letter/preview before attaching.
Once confirmed, the side letter is base64-encoded and injected into the DocuSign
envelope as an additional InlineTemplate document prepended to the sub docs.
"""

from typing import Optional

from core.database import supabase
from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

SIDE_LETTER_SYSTEM_PROMPT = """
You are a legal drafting assistant for a private investment firm.
Your task is to fill in a side letter template with the provided investor and
provision details. Follow these rules strictly:

1. Replace every {{ placeholder }} with the appropriate value.
2. Do NOT add new legal language beyond what is templated.
3. If a provision is listed, include it exactly as described in the appropriate
   template section. Reference the PPM section number if provided.
4. Keep formatting clean — use consistent spacing and capitalization.
5. Output only the completed side letter text. No commentary, no preamble.
"""


def _get_firm_side_letter_template(firm_id: str) -> str:
    """Fetch the firm's side letter template from firm_settings."""
    result = (
        supabase.table("firm_settings")
        .select("side_letter_template")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not result or not result.get("side_letter_template"):
        raise ValueError(
            "No side letter template configured. Add one to firm_settings.side_letter_template."
        )
    return result["side_letter_template"]


def generate_side_letter(
    firm_id: str,
    investor: dict,
    deal: dict,
    commitment: dict,
    provisions: list[str],
    ppm_section_reference: Optional[str] = None,
    override_mgmt_fee: Optional[float] = None,
    override_carry: Optional[float] = None,
) -> str:
    """
    Use GPT to merge the firm's side letter template with investor-specific data.

    Returns the completed side letter as a plain-text string ready for ops review.
    """
    template = _get_firm_side_letter_template(firm_id)

    committed = float(commitment.get("committed_amount") or 0)
    advisory_fee = float(commitment.get("advisory_fee_pct") or 1.0)

    # Build context for GPT
    investor_section = f"""
INVESTOR DETAILS:
  Entity Name:       {investor.get('entity_name', '')}
  Entity Type:       {investor.get('entity_type', '')}
  Mailing Address:   {investor.get('mailing_address', '')}
  Tax ID:            {investor.get('tax_id', '')}
  State/Country:     {investor.get('state_of_formation') or investor.get('country_of_formation', '')}
"""

    signatories = _get_signer_info(investor.get("id", ""))
    if signatories:
        sig = signatories[0]
        investor_section += f"""  Authorized Signer: {sig.get('name', '')}
  Signer Title:      {sig.get('title', '')}
"""

    deal_section = f"""
DEAL DETAILS:
  Fund Name:         {deal.get('offering_name', '')}
  Fund Manager:      {deal.get('fund_manager', '')}
  Fund Manager Title:{deal.get('fund_manager_title', '')}
  Commitment Amount: ${committed:,.2f}
  Advisory Fee:      {advisory_fee:.2f}%
"""
    if override_mgmt_fee is not None:
        deal_section += f"  Override Mgmt Fee: {override_mgmt_fee:.2f}%\n"
    if override_carry is not None:
        deal_section += f"  Override Carry:    {override_carry:.2f}%\n"

    provisions_section = "PROVISIONS TO INCLUDE:\n"
    for i, prov in enumerate(provisions, 1):
        provisions_section += f"  {i}. {prov}\n"
    if ppm_section_reference:
        provisions_section += f"\nPPM Reference Section: {ppm_section_reference}\n"

    user_message = f"""
Please complete the following side letter template using the details provided below.

{investor_section}
{deal_section}
{provisions_section}

TEMPLATE TO COMPLETE:
---
{template}
---
"""

    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SIDE_LETTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
    )

    return response.choices[0].message.content.strip()


def _get_signer_info(investor_id: str) -> list[dict]:
    """Get the most recent confirmed qualified signatories from kyc_reviews."""
    try:
        reviews = (
            supabase.table("kyc_reviews")
            .select("signatories")
            .eq("investor_id", investor_id)
            .order("created_at", desc=True)
            .limit(3)
            .execute()
            .data
        )
        for review in reviews:
            sigs = [s for s in (review.get("signatories") or []) if s.get("qualified_to_sign")]
            if sigs:
                return sigs
    except Exception:
        pass
    return []


# Anchor strings embedded in the PDF signature page.
# DocuSign matches these via anchorString to place SignHere + DateSigned tabs.
ANCHOR_INVESTOR_SIG = "/sl_investor_sig\\"
ANCHOR_INVESTOR_NAME = "/sl_investor_name\\"
ANCHOR_CEO_SIG = "/sl_ceo_sig\\"
ANCHOR_CEO_NAME = "/sl_ceo_name\\"


def side_letter_to_pdf_bytes(text: str, entity_name: str, offering_name: str) -> bytes:
    """
    Convert the completed side letter text to a PDF.

    Always appends a dedicated signature page as the final page.
    The signature page contains anchor strings that DocuSign uses to
    position SignHere and DateSigned tabs via anchorString matching:
      - ANCHOR_INVESTOR_SIG  → investor SignHere tab
      - ANCHOR_CEO_SIG       → CEO SignHere tab

    Falls back to plain-text bytes if fpdf2 is not installed.
    """
    try:
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_margins(25, 20, 25)
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)

        # Title block
        pdf.set_font("Helvetica", style="B", size=11)
        pdf.cell(0, 8, "SIDE LETTER AGREEMENT", ln=True, align="C")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 6, f"{offering_name}  —  {entity_name}", ln=True, align="C")
        pdf.ln(8)

        # Body — split by paragraph
        for line in text.split("\n"):
            if line.strip() == "":
                pdf.ln(4)
            else:
                pdf.multi_cell(0, 6, line)

        # ── Dedicated signature page ──────────────────────────────────────────
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 8, "SIGNATURE PAGE", ln=True, align="C")
        pdf.ln(12)

        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 6, "IN WITNESS WHEREOF, the parties have executed this Side Letter Agreement.", ln=True)
        pdf.ln(16)

        # Investor block
        pdf.set_font("Helvetica", style="B", size=9)
        pdf.cell(90, 6, "INVESTOR:", ln=False)
        pdf.ln(6)
        pdf.set_font("Helvetica", size=7)
        # Anchor string — DocuSign reads this and places the SignHere tab here
        pdf.set_text_color(200, 200, 200)
        pdf.cell(0, 5, ANCHOR_INVESTOR_SIG, ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(90, 6, "_" * 45, ln=True)
        pdf.set_font("Helvetica", size=7)
        pdf.cell(0, 5, ANCHOR_INVESTOR_NAME, ln=True)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 6, f"Name: {entity_name}", ln=True)
        pdf.cell(0, 6, "Title: ________________________________", ln=True)
        pdf.cell(0, 6, "Date:  ________________________________", ln=True)

        pdf.ln(20)

        # CEO / Fund Manager block
        pdf.set_font("Helvetica", style="B", size=9)
        pdf.cell(0, 6, "FUND MANAGER / CEO:", ln=True)
        pdf.set_font("Helvetica", size=7)
        pdf.set_text_color(200, 200, 200)
        pdf.cell(0, 5, ANCHOR_CEO_SIG, ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(90, 6, "_" * 45, ln=True)
        pdf.set_font("Helvetica", size=7)
        pdf.cell(0, 5, ANCHOR_CEO_NAME, ln=True)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 6, "Name:  ________________________________", ln=True)
        pdf.cell(0, 6, "Title: ________________________________", ln=True)
        pdf.cell(0, 6, "Date:  ________________________________", ln=True)

        return pdf.output(dest="S").encode("latin-1")

    except ImportError:
        # fpdf2 not installed — embed anchor strings in plain text
        sig_block = (
            f"\n\nSIGNATURE PAGE\n{'='*60}\n\n"
            f"IN WITNESS WHEREOF, the parties have executed this Side Letter Agreement.\n\n"
            f"INVESTOR:\n"
            f"{ANCHOR_INVESTOR_SIG}\n"
            f"_______________________________________________\n"
            f"{ANCHOR_INVESTOR_NAME}\n"
            f"Name: {entity_name}\n"
            f"Title: _______________________________________________\n"
            f"Date:  _______________________________________________\n\n"
            f"FUND MANAGER / CEO:\n"
            f"{ANCHOR_CEO_SIG}\n"
            f"_______________________________________________\n"
            f"{ANCHOR_CEO_NAME}\n"
            f"Name:  _______________________________________________\n"
            f"Title: _______________________________________________\n"
            f"Date:  _______________________________________________\n"
        )
        header = f"SIDE LETTER — {offering_name} — {entity_name}\n{'='*60}\n\n"
        return (header + text + sig_block).encode("utf-8")
