"""
Transfer of Interest routes.

POST   /transfers                — create a new transfer record (ops-initiated)
GET    /transfers/{id}           — fetch transfer details and status
PATCH  /transfers/{id}           — update status or notes manually
POST   /transfers/{id}/docusign  — send DocuSign TOI envelope (requires firm TOI template)
GET    /transfers/deal/{deal_id} — list all transfers for a deal
"""

import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.database import supabase

router = APIRouter()

VALID_STATUSES = ("Pending", "DocuSign Sent", "Executed", "Complete", "Cancelled")


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _get_transfer(transfer_id: str, firm_id: str) -> dict:
    result = (
        supabase.table("transfers_of_interest")
        .select(
            "*, "
            "commitments(id, committed_amount, deals(offering_name)), "
            "transferor:transferor_investor_id(entity_name, primary_email, advisor_email), "
            "transferee:transferee_investor_id(entity_name, primary_email, advisor_email)"
        )
        .eq("id", transfer_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Transfer of Interest not found.")
    return result.data


class CreateTransferPayload(BaseModel):
    commitment_id: str
    transferor_investor_id: str
    transferee_investor_id: str
    transfer_amount: float
    transfer_date: Optional[str] = None
    notes: Optional[str] = None


class UpdateTransferPayload(BaseModel):
    status: Optional[str] = None
    transfer_date: Optional[str] = None
    notes: Optional[str] = None


@router.post("")
def create_transfer(
    payload: CreateTransferPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Create a new Transfer of Interest record.
    Links a transferor investor to a transferee investor for a specific commitment.
    Both investors must already exist in the investors table.
    """
    firm_id = _require_firm(x_firm_id)

    # Validate commitment exists and belongs to this firm
    commitment = (
        supabase.table("commitments")
        .select("id, committed_amount, deals(offering_name)")
        .eq("id", payload.commitment_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found.")

    if payload.transfer_amount > commitment["committed_amount"]:
        raise HTTPException(
            status_code=400,
            detail=f"Transfer amount ${payload.transfer_amount:,.2f} exceeds commitment amount ${commitment['committed_amount']:,.2f}.",
        )

    # Validate both investors exist
    for investor_id, label in [
        (payload.transferor_investor_id, "Transferor"),
        (payload.transferee_investor_id, "Transferee"),
    ]:
        inv = (
            supabase.table("investors")
            .select("id")
            .eq("id", investor_id)
            .eq("firm_id", firm_id)
            .single()
            .execute()
            .data
        )
        if not inv:
            raise HTTPException(status_code=404, detail=f"{label} investor not found.")

    result = supabase.table("transfers_of_interest").insert({
        "firm_id": firm_id,
        "commitment_id": payload.commitment_id,
        "transferor_investor_id": payload.transferor_investor_id,
        "transferee_investor_id": payload.transferee_investor_id,
        "transfer_amount": payload.transfer_amount,
        "transfer_date": payload.transfer_date,
        "status": "Pending",
        "notes": payload.notes,
    }).execute()

    return {"status": "created", "transfer": result.data[0]}


@router.get("/deal/{deal_id}")
def list_transfers_for_deal(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """List all Transfers of Interest for a deal."""
    firm_id = _require_firm(x_firm_id)

    # Get all commitment IDs for this deal
    commitments = (
        supabase.table("commitments")
        .select("id")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )
    commitment_ids = [c["id"] for c in commitments]

    if not commitment_ids:
        return {"transfers": []}

    transfers = (
        supabase.table("transfers_of_interest")
        .select("*")
        .in_("commitment_id", commitment_ids)
        .eq("firm_id", firm_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return {"transfers": transfers}


@router.get("/{transfer_id}")
def get_transfer(
    transfer_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Fetch full Transfer of Interest details including both investor parties."""
    firm_id = _require_firm(x_firm_id)
    transfer = _get_transfer(transfer_id, firm_id)
    return transfer


@router.patch("/{transfer_id}")
def update_transfer(
    transfer_id: str,
    payload: UpdateTransferPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Manually update transfer status, date, or notes.
    Used for managing transfers when DocuSign is not used, or before / after an automated send.
    """
    firm_id = _require_firm(x_firm_id)
    _get_transfer(transfer_id, firm_id)

    if payload.status and payload.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{payload.status}'. Valid options: {list(VALID_STATUSES)}",
        )

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        return {"status": "no_changes", "transfer_id": transfer_id}

    result = supabase.table("transfers_of_interest").update(updates).eq("id", transfer_id).execute()
    return {"status": "updated", "transfer": result.data[0]}


def _get_firm_settings(firm_id: str) -> dict:
    result = supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


@router.post("/{transfer_id}/docusign")
def send_toi_docusign(
    transfer_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Send DocuSign TOI envelope (Transferor routing 1 → Transferee routing 2).
    Requires `firm_settings.docusign_toi_template_id` (or env `DOCUSIGN_TOI_TEMPLATE_ID`).
    Template roles must be named **Transferor** and **Transferee** with optional text tabs
    documented in `send_toi_envelope` in `core/docusign_client.py`.
    """
    firm_id = _require_firm(x_firm_id)
    transfer = _get_transfer(transfer_id, firm_id)

    if transfer.get("status") not in ("Pending",):
        raise HTTPException(
            status_code=409,
            detail=f"DocuSign send is only allowed from status Pending (current: {transfer.get('status')}).",
        )
    if transfer.get("toi_envelope_id"):
        raise HTTPException(
            status_code=409,
            detail="A TOI envelope was already dispatched for this transfer.",
        )

    settings = _get_firm_settings(firm_id)

    if not settings.get("docusign_toi_template_id") and not os.environ.get("DOCUSIGN_TOI_TEMPLATE_ID"):
        raise HTTPException(
            status_code=422,
            detail="docusign_toi_template_id is not configured in firm settings (or DOCUSIGN_TOI_TEMPLATE_ID in env).",
        )

    transferor = transfer.get("transferor") or {}
    transferee = transfer.get("transferee") or {}
    if not transferor.get("primary_email") or not transferee.get("primary_email"):
        raise HTTPException(
            status_code=422,
            detail="Both transferor and transferee must have primary_email before sending TOI.",
        )

    comm = transfer.get("commitments") or {}
    if isinstance(comm, list):
        comm = comm[0] if comm else {}
    deal = comm.get("deals") or {}
    if isinstance(deal, list):
        deal = deal[0] if deal else {}

    from core.docusign_client import send_toi_envelope

    try:
        result_ds = send_toi_envelope(
            settings=settings,
            transfer=transfer,
            transferor=transferor,
            transferee=transferee,
            deal=deal,
            commitment=comm,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    envelope_id = result_ds["envelope_id"]
    supabase.table("transfers_of_interest").update({
        "toi_envelope_id": envelope_id,
        "status": "DocuSign Sent",
    }).eq("id", transfer_id).execute()

    return {
        "status": "sent",
        "transfer_id": transfer_id,
        "toi_envelope_id": envelope_id,
    }
