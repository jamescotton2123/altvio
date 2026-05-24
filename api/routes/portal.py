"""
Investor Portal routes.

KYC Upload Portal:
  GET  /portal/kyc/{token}               — entity-type upload checklist (HTML)
  POST /portal/kyc/{token}/upload        — receive file, save to SharePoint, trigger KYC parser

Investor Document Portal:
  GET  /portal/view/{token}              — full capital account + documents (HTML)
  GET  /portal/download/{token}/{type}   — proxied document download
  GET  /portal/login                     — passwordless login page (HTML)
  POST /portal/request-access            — investor enters email → receives magic link
  POST /portal/action/{token}/loi-request       — submit interest in an open deal
  POST /portal/action/{token}/wire-change-request — request wire instruction update

Internal / Ops:
  POST /portal/generate-link             — generate document portal token for a commitment
"""

import logging
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.auth import intake_key_limiter, resolve_firm_from_intake_key
from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.portal import (
    assemble_kyc_portal_data,
    assemble_portal_data,
    generate_portal_token,
    request_portal_access,
    validate_kyc_token,
    validate_portal_token,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    return result.data or {}


def _get_settings_by_subdomain(subdomain: str) -> tuple[str | None, dict]:
    """Look up firm_id and settings by portal_subdomain."""
    result = (
        supabase.table("firm_settings")
        .select("*")
        .eq("portal_subdomain", subdomain)
        .single()
        .execute()
    )
    if not result.data:
        return None, {}
    return result.data.get("firm_id"), result.data


# ---------------------------------------------------------------------------
# Internal token generation
# ---------------------------------------------------------------------------

class GenerateLinkPayload(BaseModel):
    firm_id: Optional[str] = None
    investor_id: str
    commitment_id: str


@router.post("/generate-link")
@intake_key_limiter.limit("60/minute")
def generate_link(
    request: Request,
    payload: GenerateLinkPayload,
    x_pivot_intake_key: Optional[str] = Header(default=None),
):
    firm_id = resolve_firm_from_intake_key(x_pivot_intake_key or "")
    if payload.firm_id and payload.firm_id != firm_id:
        raise HTTPException(status_code=422, detail="firm_id does not match intake key.")

    settings = _get_settings(firm_id)
    expiry_days = settings.get("portal_link_expiry_days") or 30
    result = generate_portal_token(
        firm_id=firm_id,
        investor_id=payload.investor_id,
        commitment_id=payload.commitment_id,
        expiry_days=expiry_days,
        settings=settings,
    )
    return result


# ---------------------------------------------------------------------------
# Passwordless re-auth
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(subdomain: Optional[str] = None):
    """
    Render the investor login page. Subdomain can be passed as a query param
    or detected from the Host header in production (handled by reverse proxy).
    """
    return HTMLResponse(content=_render_login_page(subdomain or ""))


@router.post("/request-access")
async def request_access(request: Request):
    """
    Investor submits their email. Platform looks them up by email + firm,
    generates a fresh magic link, and emails it to them.
    """
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    subdomain = (body.get("subdomain") or "").strip()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")

    firm_id, settings = _get_settings_by_subdomain(subdomain) if subdomain else (None, {})

    # Fallback: try X-Firm-ID header
    if not firm_id:
        firm_id = request.headers.get("x-firm-id")
        if firm_id:
            settings = _get_settings(firm_id)

    if not firm_id:
        raise HTTPException(status_code=400, detail="Could not identify firm. Please use your firm's portal link.")

    request_portal_access(email=email, firm_id=firm_id, settings=settings)
    # Always return 200 to prevent email enumeration
    return {
        "status": "ok",
        "message": "If an account was found, a secure link has been sent to your email address.",
    }


# ---------------------------------------------------------------------------
# KYC upload portal
# ---------------------------------------------------------------------------

@router.get("/kyc/{token}", response_class=HTMLResponse)
def kyc_upload_page(token: str):
    record = validate_kyc_token(token)
    if not record:
        return HTMLResponse(content=_render_expired_page(), status_code=410)
    settings = _get_settings(record["firm_id"])
    data = assemble_kyc_portal_data(record, settings)
    return HTMLResponse(content=_render_kyc_page(data))


@router.post("/kyc/{token}/upload")
async def kyc_upload(token: str, file: UploadFile = File(...), doc_label: str = Form(default="")):
    """
    Receive a KYC document upload from the investor portal.
    Saves to their SharePoint KYC folder and triggers the KYC parser.
    """
    record = validate_kyc_token(token)
    if not record:
        raise HTTPException(status_code=410, detail="This upload link has expired or is no longer valid.")

    investor = record.get("investors") or {}
    settings = _get_settings(record["firm_id"])

    filename = file.filename or "document.pdf"
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file received.")

    # Save to SharePoint investor KYC folder
    folder_id = investor.get("sharepoint_folder_id")
    if folder_id:
        try:
            from core.graph_client import save_document_to_folder
            save_document_to_folder(settings, folder_id, filename, file_bytes)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save document: {e}")

    # Trigger KYC parser (same logic as the SharePoint webhook)
    try:
        from core.kyc_parser import process_kyc_upload
        base_url = settings.get("portal_subdomain", "")
        source_doc_url = f"https://{base_url}.pivotops.pro/kyc/{token}" if base_url else None
        process_kyc_upload(
            investor_id=record["investor_id"],
            firm_id=record["firm_id"],
            filename=filename,
            file_bytes=file_bytes,
            settings=settings,
            source_doc_url=source_doc_url,
        )
    except Exception as e:
        logger.warning("Portal KYC parser failed: %s", e)

    return {
        "status": "uploaded",
        "filename": filename,
        "doc_label": doc_label,
        "investor": investor.get("entity_name"),
    }


# ---------------------------------------------------------------------------
# Investor document portal
# ---------------------------------------------------------------------------

@router.get("/view/{token}", response_class=HTMLResponse)
def view_portal(token: str):
    record = validate_portal_token(token)
    if not record:
        return HTMLResponse(content=_render_expired_page(), status_code=410)
    settings = _get_settings(record["firm_id"])
    data = assemble_portal_data(record, settings)
    return HTMLResponse(content=_render_portal_page(data))


@router.get("/download/{token}/{doc_type}")
def download_document(token: str, doc_type: str):
    record = validate_portal_token(token)
    if not record:
        raise HTTPException(status_code=410, detail="This link has expired or is no longer valid.")

    commitment = record.get("commitments") or {}
    investor = record.get("investors") or {}
    deal = (commitment.get("deals") or {}) if commitment else {}
    settings = _get_settings(record["firm_id"])

    if doc_type == "subdoc":
        envelope_id = commitment.get("envelope_id")
        if not envelope_id:
            raise HTTPException(status_code=404, detail="Signed documents not yet available.")
        from core.docusign_client import download_signed_documents, truncate_entity_name
        pdf_bytes = download_signed_documents(envelope_id, settings)
        entity_slug = truncate_entity_name(investor.get("entity_name", "Investor"), 35).replace(" ", "_")
        fund_slug = deal.get("offering_name", "Fund").replace(" ", "_")
        filename = f"{entity_slug}_{fund_slug}_SignedDocs.pdf"
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    if doc_type == "side_letter":
        side_letter_text = commitment.get("side_letter_notes")
        if not side_letter_text:
            raise HTTPException(status_code=404, detail="Side letter not available.")
        from core.docusign_client import truncate_entity_name
        from core.side_letter import side_letter_to_pdf_bytes
        pdf_bytes = side_letter_to_pdf_bytes(
            text=side_letter_text,
            entity_name=investor.get("entity_name", ""),
            offering_name=deal.get("offering_name", ""),
        )
        entity_slug = truncate_entity_name(investor.get("entity_name", "Investor"), 35).replace(" ", "_")
        fund_slug = deal.get("offering_name", "Fund").replace(" ", "_")
        filename = f"{entity_slug}_{fund_slug}_SideLetter.pdf"
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    if doc_type == "wire_pdf":
        wire_filename = deal.get("wire_instructions_pdf_filename")
        folder_url = deal.get("sharepoint_folder_url")
        if not wire_filename or not folder_url:
            raise HTTPException(status_code=404, detail="Wire instructions PDF not available.")
        from core.graph_client import download_file_from_folder
        pdf_bytes = download_file_from_folder(settings, folder_url, wire_filename)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{wire_filename}"'})

    raise HTTPException(status_code=400, detail=f"Unknown document type: {doc_type}")


# ---------------------------------------------------------------------------
# Self-service actions
# ---------------------------------------------------------------------------

@router.post("/action/{token}/loi-request")
async def submit_loi_request(token: str, request: Request):
    """
    Investor submits interest in an open deal from the portal.
    Creates a commitment record with loi_status='Pending', loi_source='portal'.
    Notifies ops + advisor.
    """
    record = validate_portal_token(token)
    if not record:
        raise HTTPException(status_code=410, detail="Link expired.")

    body = await request.json()
    deal_id = body.get("deal_id")
    committed_amount = float(body.get("committed_amount") or 0)
    investor_note = body.get("note", "")

    if not deal_id or committed_amount <= 0:
        raise HTTPException(status_code=400, detail="deal_id and committed_amount are required.")

    investor = record.get("investors") or {}
    firm_id = record["firm_id"]
    investor_id = record["investor_id"]
    settings = _get_settings(firm_id)

    # Verify deal belongs to this firm and is active
    deal = (
        supabase.table("deals")
        .select("id, offering_name, firm_id, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal or deal.get("status") == "Closed":
        raise HTTPException(status_code=404, detail="Deal not found or no longer active.")

    # Check for existing commitment
    existing = (
        supabase.table("commitments")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("deal_id", deal_id)
        .execute()
        .data
    )
    if existing:
        raise HTTPException(status_code=409, detail="You already have a commitment for this fund.")

    # Create LOI commitment
    new_commitment = supabase.table("commitments").insert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "deal_id": deal_id,
        "committed_amount": committed_amount,
        "loi_status": "Pending",
        "loi_source": "portal",
        "docusign_status": "Pending",
        "wire_status": "Awaiting Funds",
        "status": "Active",
    }).execute().data[0]

    inv_full = (
        supabase.table("investors")
        .select("*")
        .eq("id", investor_id)
        .single()
        .execute()
        .data
    )
    if inv_full and inv_full.get("private_wealth"):
        from core.pw_liquidation import apply_pw_liquidation_on_new_commitment

        apply_pw_liquidation_on_new_commitment(
            firm_id=firm_id,
            commitment_id=new_commitment["id"],
            committed_amount=committed_amount,
            investor=inv_full,
            deal=deal,
            settings=settings,
            send_alerts=True,
        )

    # Notify ops + advisor
    from core.graph_client import send_email
    ops_email = settings.get("ops_mailbox")
    if ops_email:
        send_email(
            settings=settings,
            to=ops_email,
            cc=[investor.get("advisor_email")] if investor.get("advisor_email") else [],
            subject=f"Portal LOI — {investor.get('entity_name')} → {deal['offering_name']}",
            body=f"""A new LOI has been submitted through the investor portal.

Investor: {investor.get('entity_name')}
Fund: {deal['offering_name']}
Commitment Amount: ${committed_amount:,.2f}
Source: Investor Portal
{f'Investor Note: {investor_note}' if investor_note else ''}

Please follow up with the investor and their advisor to initiate onboarding.
Commitment ID: {new_commitment['id']}
""",
        )

    return {
        "status": "loi_submitted",
        "commitment_id": new_commitment["id"],
        "offering_name": deal["offering_name"],
        "committed_amount": committed_amount,
        "message": "Your interest has been submitted. An advisor will be in touch shortly.",
    }


@router.post("/action/{token}/wire-change-request")
async def submit_wire_change_request(token: str, request: Request):
    """
    Investor submits distribution payout wire instructions (where we send them money on distributions).
    Not the fund's inbound subscription account. Creates investor_pending_changes — NEVER auto-applied.
    Ops must verbally verify before applying.
    """
    record = validate_portal_token(token)
    if not record:
        raise HTTPException(status_code=410, detail="Link expired.")

    body = await request.json()
    firm_id = record["firm_id"]
    investor_id = record["investor_id"]
    investor = record.get("investors") or {}
    settings = _get_settings(firm_id)

    # Block if a wire change is already pending
    existing_pending = (
        supabase.table("investor_pending_changes")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .execute()
        .data
    )
    if existing_pending:
        raise HTTPException(
            status_code=409,
            detail="A wire instruction update is already pending verification. Please contact operations if you need to make changes."
        )

    required_fields = ["bank_name", "account_name", "account_number"]
    missing = [f for f in required_fields if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    wire_data = {
        "bank_name": body.get("bank_name"),
        "account_name": body.get("account_name"),
        "account_number": body.get("account_number"),
        "routing_number": body.get("routing_number"),
        "swift_code": body.get("swift_code"),
        "iban": body.get("iban"),
        "bank_address": body.get("bank_address"),
        "further_credit": body.get("further_credit"),
        "reference": body.get("reference"),
    }

    import json
    supabase.table("investor_pending_changes").insert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "field_name": "wire_instructions",
        "proposed_value": json.dumps(wire_data),
        "source": "portal_request",
        "status": "Pending",
    }).execute()

    # Notify ops for verbal verification
    from core.graph_client import send_email
    ops_email = settings.get("ops_mailbox")
    if ops_email:
        send_email(
            settings=settings,
            to=ops_email,
            cc=[],
            subject=f"Wire Change Request — {investor.get('entity_name')} (Verbal Verification Required)",
            body=f"""A distribution payout wire instruction update has been submitted through the investor portal and requires verbal verification before it can be applied. (These are the bank details where we send distributions to you — not the fund account used for your subscription wire.)


Investor: {investor.get('entity_name')}
Email: {investor.get('primary_email', '')}

Proposed Wire Instructions:
  Bank Name: {wire_data.get('bank_name', '')}
  Account Name: {wire_data.get('account_name', '')}
  Account Number: {wire_data.get('account_number', '')}
  Routing: {wire_data.get('routing_number', '')}
  SWIFT: {wire_data.get('swift_code', '')}

ACTION REQUIRED: Please call the investor to verbally confirm these wire instructions before approving the change in the Investors dashboard.

DO NOT apply wire instruction changes without verbal confirmation. Wire fraud prevention protocol requires verbal verification for all wire updates.
""",
        )

    return {
        "status": "wire_change_submitted",
        "message": "Your wire instruction update has been submitted and is pending verification by our operations team. You will be contacted within 1 business day.",
    }


# ---------------------------------------------------------------------------
# HTML renderers
# ---------------------------------------------------------------------------

def _render_login_page(subdomain: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Investor Portal — Sign In</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #f8fafc; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; }}
    .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
            padding: 48px 40px; max-width: 420px; width: 100%; margin: 24px; }}
    h1 {{ font-size: 22px; font-weight: 700; color: #0f172a; margin-bottom: 8px; }}
    p {{ font-size: 14px; color: #64748b; line-height: 1.6; margin-bottom: 28px; }}
    label {{ font-size: 13px; font-weight: 500; color: #374151; display: block; margin-bottom: 6px; }}
    input[type=email] {{ width: 100%; padding: 10px 14px; border: 1px solid #d1d5db;
                         border-radius: 7px; font-size: 14px; outline: none; }}
    input[type=email]:focus {{ border-color: #0f172a; }}
    button {{ width: 100%; padding: 11px; background: #0f172a; color: #fff;
              border: none; border-radius: 7px; font-size: 14px; font-weight: 500;
              cursor: pointer; margin-top: 16px; }}
    button:hover {{ opacity: 0.88; }}
    .success {{ display: none; text-align: center; padding: 20px 0; }}
    .success p {{ color: #16a34a; font-size: 15px; }}
    .footer {{ margin-top: 24px; font-size: 12px; color: #94a3b8; text-align: center; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in to your portal</h1>
    <p>Enter the email address associated with your investment account. We'll send you a secure access link — no password needed.</p>
    <div id="form">
      <label for="email">Email Address</label>
      <input type="email" id="email" placeholder="you@example.com" autocomplete="email">
      <button onclick="submitRequest()">Send Secure Link</button>
    </div>
    <div class="success" id="success">
      <p>&#10003; Check your inbox</p>
      <p style="color:#64748b;font-size:13px;margin-top:8px">If your email is on file, a secure access link is on its way. Check your spam folder if you don't see it within a few minutes.</p>
    </div>
    <div class="footer">Powered by Altvio</div>
  </div>
  <script>
    async function submitRequest() {{
      const email = document.getElementById('email').value.trim();
      if (!email) return;
      document.querySelector('button').disabled = true;
      await fetch('/portal/request-access', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ email, subdomain: '{subdomain}' }})
      }});
      document.getElementById('form').style.display = 'none';
      document.getElementById('success').style.display = 'block';
    }}
    document.getElementById('email').addEventListener('keydown', e => {{
      if (e.key === 'Enter') submitRequest();
    }});
  </script>
</body>
</html>"""


def _render_kyc_page(data: dict) -> str:
    firm = data["firm"]
    investor = data["investor"]
    fund = data["fund"]
    required_docs = data["required_docs"]
    brand = firm["brand_color"]
    token = data["token"]

    doc_items = ""
    for i, doc in enumerate(required_docs):
        doc_items += f"""
        <div class="doc-item" id="doc-{i}">
          <div class="doc-info">
            <div class="doc-name">{doc}</div>
            <div class="doc-status" id="status-{i}">Not uploaded</div>
          </div>
          <label class="upload-btn" for="file-{i}">
            <input type="file" id="file-{i}" accept=".pdf,.jpg,.jpeg,.png,.tiff"
                   onchange="uploadFile(this, {i}, '{doc}')" style="display:none">
            Upload
          </label>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>KYC Document Upload — {fund['offering_name']}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #1e293b; }}
    .header {{ background: {brand}; color: #fff; padding: 20px 32px; }}
    .header h1 {{ font-size: 17px; font-weight: 600; }}
    .header p {{ font-size: 13px; opacity: 0.75; margin-top: 3px; }}
    .container {{ max-width: 680px; margin: 36px auto; padding: 0 24px 80px; }}
    .hero {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 24px 28px; margin-bottom: 24px; }}
    .hero h2 {{ font-size: 18px; font-weight: 700; color: #0f172a; }}
    .hero p {{ font-size: 14px; color: #64748b; margin-top: 6px; line-height: 1.6; }}
    .third-party-banner {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
                           padding: 12px 16px; font-size: 13px; color: #1e40af; margin-bottom: 20px; }}
    .doc-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .doc-item {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
                padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; }}
    .doc-item.uploaded {{ border-color: #86efac; background: #f0fdf4; }}
    .doc-name {{ font-size: 14px; font-weight: 500; color: #1e293b; }}
    .doc-status {{ font-size: 12px; color: #94a3b8; margin-top: 3px; }}
    .doc-item.uploaded .doc-status {{ color: #16a34a; font-weight: 500; }}
    .upload-btn {{ display: inline-block; background: {brand}; color: #fff; font-size: 13px;
                  font-weight: 500; padding: 7px 16px; border-radius: 6px; cursor: pointer;
                  white-space: nowrap; }}
    .upload-btn:hover {{ opacity: 0.88; }}
    .upload-btn.uploading {{ opacity: 0.6; cursor: not-allowed; }}
    .progress-bar {{ height: 4px; background: #e2e8f0; border-radius: 2px; margin-top: 16px; }}
    .progress-fill {{ height: 100%; background: {brand}; border-radius: 2px;
                     width: 0%; transition: width 0.3s; }}
    .footer {{ margin-top: 48px; font-size: 12px; color: #94a3b8; line-height: 1.8; }}
    .all-done {{ display: none; background: #f0fdf4; border: 1px solid #86efac; border-radius: 10px;
                padding: 24px; text-align: center; margin-top: 24px; }}
    .all-done h3 {{ font-size: 17px; font-weight: 600; color: #15803d; }}
    .all-done p {{ font-size: 14px; color: #64748b; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>{firm['name']}</h1>
    <p>KYC Document Upload — {fund['offering_name']}</p>
  </div>
  <div class="container">
    <div class="hero">
      <h2>Welcome, {investor['entity_name']}</h2>
      <p>To complete your onboarding for <strong>{fund['offering_name']}</strong>, please upload the documents listed below. All files are saved securely to your investor folder and reviewed by our compliance team.</p>
      <p style="margin-top:10px;font-size:13px;color:#94a3b8">Entity type: {investor['entity_type']} &middot; Accepted formats: PDF, JPG, PNG, TIFF</p>
    </div>

    <div class="third-party-banner">
      Uploading on behalf of {investor['entity_name']}? That's fine — please upload all required documents below. Our team will confirm receipt with the investor directly.
    </div>

    <div class="doc-list">{doc_items}</div>

    <div class="progress-bar"><div class="progress-fill" id="progress"></div></div>

    <div class="all-done" id="all-done">
      <h3>&#10003; All documents submitted</h3>
      <p>Thank you. Our compliance team will review your documents and be in touch within 2 business days. If anything is missing or needs clarification, {firm.get('ops_email', 'our team')} will reach out.</p>
    </div>

    <div class="footer">
      Questions? Contact <a href="mailto:{firm['ops_email']}" style="color:{brand}">{firm['ops_email']}</a><br>
      This secure upload link expires 14 days after it was issued. Powered by <strong>Altvio</strong>.
    </div>
  </div>
  <script>
    let uploaded = 0;
    const total = {len(required_docs)};
    async function uploadFile(input, idx, label) {{
      const file = input.files[0];
      if (!file) return;
      const btn = input.closest('label');
      btn.textContent = 'Uploading...';
      btn.classList.add('uploading');
      document.getElementById('status-' + idx).textContent = 'Uploading...';
      const form = new FormData();
      form.append('file', file);
      form.append('doc_label', label);
      try {{
        const resp = await fetch('/portal/kyc/{token}/upload', {{ method: 'POST', body: form }});
        if (resp.ok) {{
          document.getElementById('doc-' + idx).classList.add('uploaded');
          document.getElementById('status-' + idx).textContent = 'Uploaded \u2713';
          btn.textContent = 'Replace';
          btn.classList.remove('uploading');
          uploaded++;
          document.getElementById('progress').style.width = (uploaded / total * 100) + '%';
          if (uploaded >= total) document.getElementById('all-done').style.display = 'block';
        }} else {{
          document.getElementById('status-' + idx).textContent = 'Upload failed — try again';
          btn.textContent = 'Retry';
          btn.classList.remove('uploading');
        }}
      }} catch(e) {{
        document.getElementById('status-' + idx).textContent = 'Upload failed — try again';
        btn.textContent = 'Retry';
        btn.classList.remove('uploading');
      }}
    }}
  </script>
</body>
</html>"""


def _render_portal_page(data: dict) -> str:
    firm = data["firm"]
    investor = data["investor"]
    documents = data["documents"]
    capital = data.get("capital_account") or {}
    summary = capital.get("summary") or {}
    funds = capital.get("funds") or []
    dist_history = capital.get("distribution_history") or []
    open_deals = data.get("open_deals") or []
    pending_wire = data.get("pending_wire_change", False)
    brand = firm["brand_color"]
    token = data["token"]

    # Documents section
    doc_rows = ""
    for doc in documents:
        doc_rows += f"""
        <div class="doc-row">
          <div class="doc-label"><span class="doc-icon">&#128196;</span>{doc['label']}<span class="doc-fund">{doc.get('fund','')}</span></div>
          <a class="btn-sm" href="{doc['download_path']}">Download</a>
        </div>"""

    # Capital account fund rows
    fund_rows = ""
    for f in funds:
        status_color = "#16a34a" if f["docusign_status"] in SIGNED_STATES else "#d97706"
        funded_pct = int(f["funded_amount"] / f["committed_amount"] * 100) if f["committed_amount"] else 0
        fund_rows += f"""
        <tr>
          <td class="td-fund">{f['offering_name']}</td>
          <td>${f['committed_amount']:,.0f}</td>
          <td>${f['funded_amount']:,.0f}<div class="mini-bar"><div style="width:{funded_pct}%;background:{brand};height:100%;border-radius:2px"></div></div></td>
          <td>${f['distributions_received']:,.0f}</td>
          <td style="color:{status_color};font-weight:500">{f['docusign_status'] or '—'}</td>
        </tr>"""

    # Distribution history rows
    dist_rows = ""
    for d in dist_history[:10]:
        try:
            from datetime import datetime as _dt
            date_str = _dt.fromisoformat((d.get("date") or "").replace("Z", "+00:00")).strftime("%b %d, %Y")
        except Exception:
            date_str = d.get("date") or "—"
        dist_rows += f"""
        <tr><td>{date_str}</td><td>{d.get('fund','—')}</td>
        <td>{d.get('type','Distribution')}</td><td>${float(d.get('amount',0)):,.0f}</td></tr>"""

    # Open deals for LOI
    deal_options = "".join(
        f'<option value="{d["id"]}">{d["offering_name"]}{" — " + d["fund_manager"] if d.get("fund_manager") else ""}</option>'
        for d in open_deals
    )
    loi_section = ""
    if open_deals:
        loi_section = f"""
    <div class="section">
      <h2>Investment Opportunities</h2>
      <div class="card-box">
        <p style="font-size:14px;color:#64748b;margin-bottom:16px">Express interest in an open fund. Your advisor will be notified and will follow up to initiate onboarding.</p>
        <select id="loi-deal" class="field-input"><option value="">Select a fund...</option>{deal_options}</select>
        <input type="number" id="loi-amount" class="field-input" placeholder="Commitment amount ($)" min="1" style="margin-top:10px">
        <textarea id="loi-note" class="field-input" placeholder="Optional note to your advisor" rows="2" style="margin-top:10px"></textarea>
        <button class="btn-action" onclick="submitLOI()">Submit Interest</button>
        <div id="loi-msg" style="display:none;margin-top:12px;font-size:13px;color:#16a34a"></div>
      </div>
    </div>"""

    # Wire change section
    wire_section = f"""
    <div class="section">
      <h2>Wire Instructions</h2>
      <div class="card-box">
        {"<div class='pending-badge'>&#9679; Wire update pending ops verification</div>" if pending_wire else ""}
        {"<p style='font-size:13px;color:#64748b'>Need to update your wire instructions? Submit the new details below. Our operations team will call you to verify before any changes are applied.</p>" if not pending_wire else ""}
        {"" if pending_wire else '''
        <div id="wire-form">
          <div class="field-grid">
            <div><label class="field-label">Bank Name *</label><input class="field-input" id="wf-bank" placeholder="Chase, BofA..."></div>
            <div><label class="field-label">Account Name *</label><input class="field-input" id="wf-acct-name" placeholder="As it appears on the account"></div>
            <div><label class="field-label">Account Number *</label><input class="field-input" id="wf-acct-num" placeholder=""></div>
            <div><label class="field-label">ABA / Routing</label><input class="field-input" id="wf-routing" placeholder="9 digits"></div>
            <div><label class="field-label">SWIFT / BIC</label><input class="field-input" id="wf-swift" placeholder="For international wires"></div>
            <div><label class="field-label">Reference / Memo</label><input class="field-input" id="wf-ref" placeholder="Optional"></div>
          </div>
          <p style="font-size:12px;color:#94a3b8;margin-top:12px">&#128274; Changes require verbal verification and are never applied automatically.</p>
          <button class="btn-action" onclick="submitWireChange()">Submit Wire Update</button>
          <div id="wire-msg" style="display:none;margin-top:12px;font-size:13px;color:#16a34a"></div>
        </div>'''}
      </div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Investor Portal — {investor['entity_name']}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #1e293b; }}
    .header {{ background: {brand}; color: #fff; padding: 20px 32px; display: flex; align-items: center; gap: 14px; }}
    .header h1 {{ font-size: 17px; font-weight: 600; }}
    .header p {{ font-size: 13px; opacity: 0.75; margin-top: 2px; }}
    .container {{ max-width: 860px; margin: 32px auto; padding: 0 24px 80px; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }}
    .stat-card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 9px; padding: 18px 20px; }}
    .stat-card label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #94a3b8; }}
    .stat-card .val {{ font-size: 20px; font-weight: 700; color: #0f172a; margin-top: 5px; }}
    .section {{ margin-bottom: 26px; }}
    .section h2 {{ font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #64748b; margin-bottom: 12px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e2e8f0; border-radius: 9px; overflow: hidden; font-size: 13px; }}
    th {{ background: #f8fafc; padding: 10px 16px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #64748b; border-bottom: 1px solid #e2e8f0; }}
    td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; color: #334155; }}
    tr:last-child td {{ border-bottom: none; }}
    .td-fund {{ font-weight: 500; color: #0f172a; }}
    .mini-bar {{ height: 4px; background: #e2e8f0; border-radius: 2px; margin-top: 5px; width: 80px; }}
    .doc-row {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 13px 18px;
               display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }}
    .doc-label {{ font-size: 14px; font-weight: 500; display: flex; align-items: center; gap: 10px; }}
    .doc-icon {{ font-size: 16px; }}
    .doc-fund {{ font-size: 12px; color: #94a3b8; margin-left: 4px; font-weight: 400; }}
    .btn-sm {{ background: {brand}; color: #fff; font-size: 12px; font-weight: 500; padding: 6px 14px;
              border-radius: 5px; text-decoration: none; white-space: nowrap; }}
    .btn-sm:hover {{ opacity: .88; }}
    .card-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 9px; padding: 20px 24px; }}
    .field-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .field-label {{ font-size: 12px; font-weight: 500; color: #374151; display: block; margin-bottom: 5px; }}
    .field-input {{ width: 100%; padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px;
                   font-size: 13px; outline: none; font-family: inherit; resize: vertical; }}
    .field-input:focus {{ border-color: {brand}; }}
    .btn-action {{ margin-top: 14px; background: {brand}; color: #fff; border: none; border-radius: 6px;
                  font-size: 13px; font-weight: 500; padding: 9px 20px; cursor: pointer; }}
    .btn-action:hover {{ opacity: .88; }}
    .pending-badge {{ background: #fef9c3; border: 1px solid #fde047; border-radius: 6px;
                     padding: 8px 14px; font-size: 13px; color: #854d0e; margin-bottom: 12px; }}
    .footer {{ margin-top: 48px; font-size: 12px; color: #94a3b8; line-height: 1.8; border-top: 1px solid #e2e8f0; padding-top: 20px; }}
    @media (max-width: 640px) {{
      .summary-grid {{ grid-template-columns: 1fr 1fr; }}
      .field-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>{firm['name']}</h1>
      <p>{firm['tagline']}</p>
    </div>
  </div>

  <div class="container">
    <div style="margin-bottom:20px">
      <div style="font-size:20px;font-weight:700;color:#0f172a">{investor['entity_name']}</div>
      <div style="font-size:13px;color:#64748b;margin-top:3px">{investor.get('entity_type','')}</div>
    </div>

    <div class="summary-grid">
      <div class="stat-card"><label>Total Committed</label><div class="val">${summary.get('total_committed',0):,.0f}</div></div>
      <div class="stat-card"><label>Total Funded</label><div class="val">${summary.get('total_funded',0):,.0f}</div></div>
      <div class="stat-card"><label>Distributions</label><div class="val">${summary.get('total_distributions',0):,.0f}</div></div>
      <div class="stat-card"><label>Funds</label><div class="val">{summary.get('fund_count',0)}</div></div>
    </div>

    <div class="section">
      <h2>Capital Account</h2>
      <table>
        <thead><tr><th>Fund</th><th>Committed</th><th>Funded</th><th>Distributions</th><th>Status</th></tr></thead>
        <tbody>{fund_rows if fund_rows else '<tr><td colspan="5" style="color:#94a3b8;text-align:center;padding:24px">No active commitments</td></tr>'}</tbody>
      </table>
    </div>

    {"<div class='section'><h2>Distribution History</h2><table><thead><tr><th>Date</th><th>Fund</th><th>Type</th><th>Amount</th></tr></thead><tbody>" + dist_rows + "</tbody></table></div>" if dist_rows else ""}

    <div class="section">
      <h2>Your Documents</h2>
      {doc_rows if doc_rows else '<p style="color:#94a3b8;font-size:14px">Documents will appear here once your subscription is complete.</p>'}
    </div>

    {loi_section}
    {wire_section}

    <div class="footer">
      <strong>{firm['name']}</strong> &middot; <a href="mailto:{firm['ops_email']}" style="color:{brand}">{firm['ops_email']}</a><br>
      This portal is for the sole use of {investor['entity_name']}. Powered by <strong>Altvio</strong>.
    </div>
  </div>

  <script>
    async function submitLOI() {{
      const deal_id = document.getElementById('loi-deal').value;
      const amount = parseFloat(document.getElementById('loi-amount').value);
      const note = document.getElementById('loi-note').value;
      if (!deal_id || !amount) {{ alert('Please select a fund and enter a commitment amount.'); return; }}
      const resp = await fetch('/portal/action/{token}/loi-request', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ deal_id, committed_amount: amount, note }})
      }});
      const data = await resp.json();
      const msg = document.getElementById('loi-msg');
      msg.style.display = 'block';
      if (resp.ok) {{ msg.textContent = data.message; msg.style.color = '#16a34a'; }}
      else {{ msg.textContent = data.detail || 'Submission failed.'; msg.style.color = '#dc2626'; }}
    }}

    async function submitWireChange() {{
      const payload = {{
        bank_name: document.getElementById('wf-bank').value,
        account_name: document.getElementById('wf-acct-name').value,
        account_number: document.getElementById('wf-acct-num').value,
        routing_number: document.getElementById('wf-routing').value,
        swift_code: document.getElementById('wf-swift').value,
        reference: document.getElementById('wf-ref').value,
      }};
      if (!payload.bank_name || !payload.account_name || !payload.account_number) {{
        alert('Bank name, account name, and account number are required.'); return;
      }}
      const resp = await fetch('/portal/action/{token}/wire-change-request', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      const data = await resp.json();
      const msg = document.getElementById('wire-msg');
      msg.style.display = 'block';
      if (resp.ok) {{ msg.textContent = data.message; msg.style.color = '#16a34a'; document.getElementById('wire-form').style.opacity = '0.5'; }}
      else {{ msg.textContent = data.detail || 'Submission failed.'; msg.style.color = '#dc2626'; }}
    }}
  </script>
</body>
</html>"""


def _render_expired_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Link Expired</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
    .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 48px 40px;
            max-width: 440px; text-align: center; }}
    h1 {{ font-size: 20px; color: #0f172a; margin-bottom: 12px; }}
    p {{ font-size: 14px; color: #64748b; line-height: 1.7; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>This link has expired</h1>
    <p>Your document access link is no longer valid. Visit your firm's portal login page to receive a new secure link, or contact your investment manager directly.</p>
  </div>
</body>
</html>"""
