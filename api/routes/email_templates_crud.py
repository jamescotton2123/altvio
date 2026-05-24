"""
Firm-level email template CRUD — `email_templates` table.

GET    /firm/templates              — list templates for X-Firm-ID
POST   /firm/templates              — upsert by (firm_id, template_key)
PATCH  /firm/templates/{template_key}
DELETE /firm/templates/{template_key}
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core.database import supabase

router = APIRouter()


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


class EmailTemplateUpsertPayload(BaseModel):
    template_key: str = Field(..., min_length=1, max_length=128)
    subject: str = Field(..., min_length=1)
    body_html: str = Field(..., min_length=1)
    is_active: Optional[bool] = True


class EmailTemplatePatchPayload(BaseModel):
    subject: Optional[str] = None
    body_html: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("")
def list_email_templates(x_firm_id: Optional[str] = Header(default=None)):
    """Return all email template rows for the firm."""
    firm_id = _require_firm(x_firm_id)
    rows = (
        supabase.table("email_templates")
        .select("*")
        .eq("firm_id", firm_id)
        .order("template_key")
        .execute()
        .data
        or []
    )
    return {"templates": rows}


@router.post("")
def upsert_email_template(
    payload: EmailTemplateUpsertPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Create or replace a template row (unique on firm_id + template_key)."""
    firm_id = _require_firm(x_firm_id)
    row = {
        "firm_id": firm_id,
        "template_key": payload.template_key.strip(),
        "subject": payload.subject,
        "body_html": payload.body_html,
        "is_active": True if payload.is_active is None else payload.is_active,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = (
        supabase.table("email_templates")
        .upsert([row], on_conflict="firm_id,template_key")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Upsert failed.")
    return {"status": "upserted", "template": result.data[0]}


@router.patch("/{template_key}")
def patch_email_template(
    template_key: str,
    payload: EmailTemplatePatchPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Update subject, body_html, and/or is_active for one template key."""
    firm_id = _require_firm(x_firm_id)
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = (
        supabase.table("email_templates")
        .update(updates)
        .eq("firm_id", firm_id)
        .eq("template_key", template_key)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Template not found.")
    return {"status": "updated", "template": result.data[0]}


@router.delete("/{template_key}", status_code=204)
def delete_email_template(
    template_key: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Hard-delete a template row."""
    firm_id = _require_firm(x_firm_id)
    supabase.table("email_templates").delete().eq("firm_id", firm_id).eq("template_key", template_key).execute()
