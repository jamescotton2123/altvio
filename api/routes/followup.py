"""
Follow-up approval routes.
Advisors click an approval link → these endpoints execute the follow-up email.

POST /followup/{token}/approve
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from core.database import supabase
from core.followup_tokens import validate_followup_token

router = APIRouter()


def _get_firm_settings(firm_id: str) -> dict:
    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    return settings or {}


@router.post("/{token}/approve", response_class=HTMLResponse)
def approve_followup(token: str):
    """
    Advisor-approved follow-up endpoint.
    Posting the one-time token from the advisor ping email sends the follow-up.
    Returns a simple HTML confirmation page.
    """
    from core.followup_scheduler import send_kyc_followup, send_wire_followup

    token_row = validate_followup_token(token)
    if not token_row:
        raise HTTPException(status_code=400, detail="Invalid or expired follow-up token.")

    followup_type = token_row.get("type")
    firm_id = token_row["firm_id"]
    settings = _get_firm_settings(firm_id)

    try:
        if followup_type == "kyc":
            investor_id = token_row.get("investor_id")
            if not investor_id:
                raise HTTPException(status_code=400, detail="Token is missing investor_id for KYC follow-up.")
            send_kyc_followup(investor_id=investor_id, firm_id=firm_id, settings=settings)
            message = "KYC follow-up email has been sent to the investor."

        elif followup_type == "wire":
            commitment_id = token_row.get("commitment_id")
            if not commitment_id:
                raise HTTPException(status_code=400, detail="Token is missing commitment_id for wire follow-up.")
            send_wire_followup(commitment_id=commitment_id, firm_id=firm_id, settings=settings)
            message = "Wire reminder email has been sent to the investor."

        else:
            raise HTTPException(status_code=400, detail=f"Unknown follow-up type: {followup_type}")

    except HTTPException:
        raise
    except Exception as e:
        return HTMLResponse(content=f"""
        <html><body style="font-family:sans-serif;padding:40px;">
        <h2 style="color:#dc2626;">Error</h2>
        <p>{str(e)}</p>
        </body></html>
        """, status_code=500)

    return HTMLResponse(content=f"""
    <html><body style="font-family:sans-serif;padding:40px;max-width:500px;margin:auto;">
    <h2 style="color:#16a34a;">Follow-up Sent</h2>
    <p>{message}</p>
    <p style="color:#6b7280;font-size:14px;">You can close this window.</p>
    </body></html>
    """)
