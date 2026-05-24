"""
Wire template generation routes.

POST /wire/distribution/{distribution_id}  — generate distribution wire upload file
POST /wire/capital-call/{capital_call_id}  — generate capital call wire upload file
GET  /wire/download/{firm_slug}/{filename} — secure file download for ops
GET  /wire/banks                           — list supported bank adapters
POST /wire/learn-template                  — upload a sample CSV and learn a custom bank format
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.database import supabase
from core.wire_template_generator import (
    BANK_ADAPTERS,
    generate_capital_call_wire_file,
    generate_distribution_wire_file,
)

router = APIRouter()

EXPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "exports"


def _get_firm(firm_id: str) -> dict:
    result = supabase.table("firms").select("id, slug").eq("id", firm_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm not found.")
    return result.data


def _get_firm_settings(firm_id: str) -> dict:
    result = (
        supabase.table("firm_settings")
        .select("wire_bank, custom_bank_name, custom_wire_column_map")
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


@router.get("/banks")
def list_supported_banks():
    """Return the list of supported bank adapter keys (including 'custom' if configured)."""
    banks = list(BANK_ADAPTERS.keys()) + ["custom"]
    return {"supported_banks": banks}


@router.post("/learn-template")
async def learn_bank_template(
    x_firm_id: Optional[str] = Header(default=None),
    bank_name: str = Form(..., description="Display name for this bank, e.g. 'First Republic'"),
    file: UploadFile = File(..., description="Sample wire CSV from the bank's portal"),
):
    """
    Upload a sample wire CSV from any bank's online portal.
    GPT-4o analyzes the headers and sample rows, maps them to internal wire fields,
    saves the mapping to firm_settings, and activates the custom adapter for future exports.

    Returns the learned column mapping so ops can verify it before generating files.
    """
    firm_id = _require_firm(x_firm_id)

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    csv_bytes = await file.read()
    if not csv_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    from core.bank_template_learner import learn_bank_template, save_bank_template

    try:
        column_map = learn_bank_template(csv_bytes)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    save_bank_template(firm_id=firm_id, bank_name=bank_name, column_map=column_map)

    return {
        "status": "template_learned",
        "bank_name": bank_name,
        "column_mapping": column_map,
        "message": (
            f"Custom bank template for '{bank_name}' has been saved. "
            "All future wire exports for this firm will use this format. "
            "Generate a test file to verify before distributing."
        ),
    }


@router.post("/distribution/{distribution_id}")
def generate_distribution_wire(
    distribution_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    wire_bank: Optional[str] = None,
):
    """
    Generate a bank-formatted wire upload CSV for a distribution.
    Uses firm_settings.wire_bank unless overridden by the wire_bank query param.
    Returns the filename for download.
    """
    firm_id = _require_firm(x_firm_id)
    firm = _get_firm(firm_id)
    settings = _get_firm_settings(firm_id)

    bank = wire_bank or settings.get("wire_bank", "generic")
    custom_map = settings.get("custom_wire_column_map") if bank == "custom" else None

    if bank not in list(BANK_ADAPTERS.keys()) + ["custom"]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported bank '{bank}'. Supported: {list(BANK_ADAPTERS.keys()) + ['custom']}",
        )

    try:
        file_path = generate_distribution_wire_file(
            distribution_id=distribution_id,
            firm_id=firm_id,
            firm_slug=firm["slug"],
            wire_bank=bank,
            custom_column_map=custom_map,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "status": "generated",
        "filename": file_path.name,
        "download_url": f"/wire/download/{firm['slug']}/{file_path.name}",
        "wire_bank": bank,
    }


@router.post("/capital-call/{capital_call_id}")
def generate_capital_call_wire(
    capital_call_id: str,
    x_firm_id: Optional[str] = Header(default=None),
    wire_bank: Optional[str] = None,
):
    """
    Generate a bank-formatted wire upload CSV for a capital call.
    Each row represents one investor's required wire to the fund.
    """
    firm_id = _require_firm(x_firm_id)
    firm = _get_firm(firm_id)
    settings = _get_firm_settings(firm_id)

    bank = wire_bank or settings.get("wire_bank", "generic")
    custom_map = settings.get("custom_wire_column_map") if bank == "custom" else None

    if bank not in list(BANK_ADAPTERS.keys()) + ["custom"]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported bank '{bank}'. Supported: {list(BANK_ADAPTERS.keys()) + ['custom']}",
        )

    try:
        file_path = generate_capital_call_wire_file(
            capital_call_id=capital_call_id,
            firm_id=firm_id,
            firm_slug=firm["slug"],
            wire_bank=bank,
            custom_column_map=custom_map,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "status": "generated",
        "filename": file_path.name,
        "download_url": f"/wire/download/{firm['slug']}/{file_path.name}",
        "wire_bank": bank,
    }


@router.get("/download/{firm_slug}/{filename}")
def download_wire_file(firm_slug: str, filename: str, x_firm_id: Optional[str] = Header(default=None)):
    """
    Secure file download endpoint for generated wire CSV files.
    Validates that the requesting firm_id matches the firm_slug in the path.
    """
    firm_id = _require_firm(x_firm_id)

    # Verify the firm_slug matches the requesting firm
    firm = _get_firm(firm_id)
    if firm["slug"] != firm_slug:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Prevent path traversal
    safe_filename = Path(filename).name
    file_path = EXPORTS_DIR / firm_slug / safe_filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found. It may have expired or not been generated yet.")

    return FileResponse(
        path=str(file_path),
        filename=safe_filename,
        media_type="text/csv",
    )
