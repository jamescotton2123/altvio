"""
Billing materialization routes (metered usage + draft invoices).

POST /billing/materialize-period — X-Firm-ID + JSON { "billing_period": "2026-05" }
GET  /billing/invoices?billing_period=2026-05 — latest draft/finalized invoice for firm
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from core.billing import materialize_billing_period
from core.database import supabase

router = APIRouter()


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


class MaterializePayload(BaseModel):
    billing_period: str


@router.post("/materialize-period")
def post_materialize_period(
    payload: MaterializePayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Build billing_usage rows (onboarding + AIP quarterly snapshot) and upsert a draft
    billing_invoices row for the given period bucket.
    """
    firm_id = _require_firm(x_firm_id)
    result = materialize_billing_period(firm_id, payload.billing_period.strip())
    return {"status": "ok", **result}


@router.get("/invoices")
def get_invoice_for_period(
    billing_period: str = Query(..., description="Same bucket passed to materialize, e.g. 2026-05"),
    x_firm_id: Optional[str] = Header(default=None),
):
    firm_id = _require_firm(x_firm_id)
    row = (
        supabase.table("billing_invoices")
        .select("*")
        .eq("firm_id", firm_id)
        .eq("billing_period", billing_period)
        .limit(1)
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="No invoice for this firm and period.")
    return row.data[0]
