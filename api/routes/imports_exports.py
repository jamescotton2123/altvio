"""
Imports & Exports — UI-facing endpoints for:

  POST /deals/{deal_id}/orion-export
    Re-run Orion NAImport for a closed/dissolved deal. Returns CSV strings
    in JSON so the UI can trigger a download as a Blob without any file
    storage on the server.

  POST /distributions/{distribution_id}/aip-export
    Re-run Orion AIP files for a past distribution. Same pattern as above.

  POST /import/csv
    Bulk import existing investors (and optionally commitments) from a CSV
    uploaded by a firm migrating from SharePoint lists or Excel. Supports
    dry-run preview before commit.

The Orion exports auto-fire on /deals/{id}/close and /deals/distributions.
These manual endpoints are for re-exporting after fixes (e.g., Orion ID
backfills, fee mode changes).
"""

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from core.audit import log_audit
from core.database import supabase
from core.orion_matcher import confirm_match

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _get_investor(investor_id: str, firm_id: str) -> dict:
    result = (
        supabase.table("investors")
        .select("id, entity_name, orion_match_status, orion_household_name, orion_review_notes")
        .eq("id", investor_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Investor not found.")
    return result.data


def _fetch_orion_candidates(firm_id: str, investor_ids: list[str]) -> dict[str, dict]:
    """Return investor_id → candidate row. Empty dict if table unavailable."""
    if not investor_ids:
        return {}
    try:
        rows = (
            supabase.table("orion_match_candidates")
            .select("investor_id, candidates, created_at")
            .eq("firm_id", firm_id)
            .in_("investor_id", investor_ids)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("Could not load orion_match_candidates: %s", exc)
        return {}
    return {row["investor_id"]: row for row in rows}


def _top_candidates(raw: list | None, limit: int = 3) -> list[dict]:
    if not raw:
        return []
    out = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        score = item.get("score")
        if name is not None:
            out.append({"name": name, "score": score})
    return out


def _rows_to_csv_string(rows: list[dict]) -> str:
    """Render a list of dicts as a CSV string. Returns empty string if no rows."""
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Orion household review queue
# ---------------------------------------------------------------------------

class OrionConfirmBody(BaseModel):
    household_name: str = Field(min_length=1)


class OrionRejectBody(BaseModel):
    reason: Optional[str] = None


@router.get("/orion/review-queue")
def get_orion_review_queue(
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Investors with unresolved Orion household matches, plus top candidate households.
    """
    firm_id = _require_firm(x_firm_id)

    investors = (
        supabase.table("investors")
        .select("id, entity_name, orion_match_status, created_at")
        .eq("firm_id", firm_id)
        .in_("orion_match_status", ["Needs Review", "No Match Found"])
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )

    candidate_rows = _fetch_orion_candidates(firm_id, [inv["id"] for inv in investors])

    return [
        {
            "investor_id": inv["id"],
            "entity_name": inv.get("entity_name"),
            "current_orion_match_status": inv.get("orion_match_status"),
            "candidates": _top_candidates(
                (candidate_rows.get(inv["id"]) or {}).get("candidates")
            ),
            "created_at": (candidate_rows.get(inv["id"]) or {}).get("created_at")
            or inv.get("created_at"),
        }
        for inv in investors
    ]


@router.post("/orion/review-queue/{investor_id}/confirm")
def confirm_orion_review_match(
    investor_id: str,
    body: OrionConfirmBody,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Ops confirms a household name for an investor in the review queue."""
    firm_id = _require_firm(x_firm_id)
    before = _get_investor(investor_id, firm_id)

    confirm_match(investor_id=investor_id, confirmed_name=body.household_name)

    log_audit(
        firm_id=firm_id,
        actor_type="ops",
        actor_id=None,
        entity_type="investor",
        entity_id=investor_id,
        action="orion_match.confirmed",
        before={
            "orion_match_status": before.get("orion_match_status"),
            "orion_household_name": before.get("orion_household_name"),
        },
        after={
            "orion_match_status": "Confirmed",
            "orion_household_name": body.household_name,
        },
    )

    return {
        "investor_id": investor_id,
        "orion_match_status": "Confirmed",
        "orion_household_name": body.household_name,
    }


@router.post("/orion/review-queue/{investor_id}/reject")
def reject_orion_review_match(
    investor_id: str,
    body: OrionRejectBody,
    x_firm_id: Optional[str] = Header(default=None),
):
    """Mark investor as no Orion match; optional reason stored on orion_review_notes."""
    firm_id = _require_firm(x_firm_id)
    before = _get_investor(investor_id, firm_id)

    update_payload: dict = {"orion_match_status": "No Match Found"}
    if body.reason is not None:
        update_payload["orion_review_notes"] = body.reason

    supabase.table("investors").update(update_payload).eq("id", investor_id).eq(
        "firm_id", firm_id
    ).execute()

    try:
        supabase.table("orion_match_candidates").update({"status": "Rejected"}).eq(
            "investor_id", investor_id
        ).eq("firm_id", firm_id).execute()
    except Exception as exc:
        logger.warning("Could not update orion_match_candidates on reject: %s", exc)

    log_audit(
        firm_id=firm_id,
        actor_type="ops",
        actor_id=None,
        entity_type="investor",
        entity_id=investor_id,
        action="orion_match.rejected",
        before={"orion_match_status": before.get("orion_match_status")},
        after=update_payload,
        metadata={"reason": body.reason} if body.reason else None,
    )

    return {
        "investor_id": investor_id,
        "orion_match_status": "No Match Found",
        "orion_review_notes": body.reason,
    }


# ---------------------------------------------------------------------------
# 1. Orion NAImport re-export for a closed deal
# ---------------------------------------------------------------------------

@router.post("/deals/{deal_id}/orion-export")
def export_deal_to_orion(
    deal_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Re-generate Orion NAImport_New + NAImport_Existing CSV files for a deal.
    Returns the CSVs as inline JSON strings so the UI can offer a download
    without touching the server filesystem.

    Blocks if any investor in the deal has unresolved Orion household matches.
    """
    from scripts.orion_export import (
        calculate_fee,
        get_deal_commitments,
        validate_no_pending_matches,
    )

    firm_id = _require_firm(x_firm_id)

    deal = (
        supabase.table("deals")
        .select("id, offering_name, status")
        .eq("id", deal_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")

    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not settings:
        raise HTTPException(status_code=404, detail="Firm settings not found.")

    commitments = get_deal_commitments(deal_id, firm_id)
    if not commitments:
        raise HTTPException(status_code=400, detail="No active commitments to export.")

    try:
        validate_no_pending_matches(commitments)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    fee_mode = settings.get("orion_aip_fee_mode", "separate_rows")
    fee_type_code = settings.get("orion_aip_fee_type_code", "MGMT_FEE")
    commit_type_code = settings.get("orion_aip_commitment_type_code", "COMMITMENT")
    offering_name = deal["offering_name"]

    # Build NAImport_New rows
    new_rows: list[dict] = []
    for c in commitments:
        inv = c.get("investors", {}) or {}
        if inv.get("orion_id") and not inv.get("orion_is_new_household"):
            continue
        committed = float(c.get("committed_amount") or 0)
        fee = calculate_fee(committed, settings)
        base = {
            "EntityName": inv.get("entity_name", ""),
            "EntityType": inv.get("entity_type", ""),
            "TaxID": inv.get("tax_id", ""),
            "PrimaryEmail": inv.get("primary_email", ""),
            "MailingAddress": inv.get("mailing_address", ""),
            "FundName": offering_name,
            "CommitmentAmount": committed if fee_mode == "separate_rows" else round(committed + fee, 2),
            "TransactionTypeCode": commit_type_code,
            "AdvisorEmail": inv.get("advisor_email", ""),
        }
        new_rows.append(base)
        if fee_mode == "separate_rows" and fee > 0:
            new_rows.append({**base, "CommitmentAmount": fee, "TransactionTypeCode": fee_type_code})

    # Build NAImport_Existing rows
    existing_rows: list[dict] = []
    for c in commitments:
        inv = c.get("investors", {}) or {}
        if not inv.get("orion_id"):
            continue
        if inv.get("orion_match_status") != "Confirmed":
            continue
        if inv.get("orion_is_new_household"):
            continue
        committed = float(c.get("committed_amount") or 0)
        fee = calculate_fee(committed, settings)
        base = {
            "OrionID": inv.get("orion_id", ""),
            "HouseholdName": inv.get("orion_household_name", ""),
            "FundName": offering_name,
            "CommitmentAmount": committed if fee_mode == "separate_rows" else round(committed + fee, 2),
            "TransactionTypeCode": commit_type_code,
        }
        existing_rows.append(base)
        if fee_mode == "separate_rows" and fee > 0:
            existing_rows.append({**base, "CommitmentAmount": fee, "TransactionTypeCode": fee_type_code})

    new_csv = _rows_to_csv_string(new_rows)
    existing_csv = _rows_to_csv_string(existing_rows)

    log_audit(
        firm_id=firm_id,
        actor_type="ops",
        actor_id=None,
        entity_type="deal",
        entity_id=deal_id,
        action="orion_naimport_exported",
        diff={
            "new_row_count": len(new_rows),
            "existing_row_count": len(existing_rows),
            "fee_mode": fee_mode,
        },
    )

    return {
        "deal_id": deal_id,
        "offering_name": offering_name,
        "naimport_new": {
            "filename": f"NAImport_New_{offering_name.replace(' ', '_')}.csv",
            "row_count": len(new_rows),
            "csv": new_csv,
        },
        "naimport_existing": {
            "filename": f"NAImport_Existing_{offering_name.replace(' ', '_')}.csv",
            "row_count": len(existing_rows),
            "csv": existing_csv,
        },
    }


# ---------------------------------------------------------------------------
# 2. Orion AIP re-export for a distribution
# ---------------------------------------------------------------------------

@router.post("/distributions/{distribution_id}/aip-export")
def export_distribution_to_aip(
    distribution_id: str,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Re-generate Orion AIPTransaction + AIPAsset CSVs for a past distribution.
    Useful after fixing a column mapping or fee_mode change.
    """
    from scripts.orion_aip_distribution_export import generate_distribution_aip

    firm_id = _require_firm(x_firm_id)

    distribution = (
        supabase.table("distributions")
        .select("id, deal_id, total_amount, distribution_date, distribution_type")
        .eq("id", distribution_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not distribution:
        raise HTTPException(status_code=404, detail="Distribution not found.")

    try:
        result = generate_distribution_aip(
            distribution_id=distribution_id,
            firm_id=firm_id,
            tpa_confirmed_total=float(distribution["total_amount"]),
            is_dissolution=(distribution.get("distribution_type") == "Return of Capital"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AIP export failed: {e}") from e

    log_audit(
        firm_id=firm_id,
        actor_type="ops",
        actor_id=None,
        entity_type="distribution",
        entity_id=distribution_id,
        action="orion_aip_exported",
        diff={"distribution_date": distribution.get("distribution_date")},
    )

    # generate_distribution_aip writes to disk and returns file paths.
    # Read them back into the response as CSV strings so the UI can download.
    aip_transaction_path = result.get("aip_transaction")
    aip_asset_path = result.get("aip_asset")

    def _read_or_empty(path: Optional[str]) -> str:
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            logger.warning("Could not read AIP file %s: %s", path, e)
            return ""

    return {
        "distribution_id": distribution_id,
        "distribution_date": distribution.get("distribution_date"),
        "aip_transaction": {
            "filename": f"AIPTransaction_{distribution['distribution_date']}.csv",
            "csv": _read_or_empty(aip_transaction_path),
        },
        "aip_asset": {
            "filename": f"AIPAsset_{distribution['distribution_date']}.csv",
            "csv": _read_or_empty(aip_asset_path),
        },
    }


# ---------------------------------------------------------------------------
# 3. CSV import for firms migrating from SharePoint / Excel
# ---------------------------------------------------------------------------

# Column header → investors column. Tolerant of common variants.
INVESTOR_COLUMN_MAP = {
    "entity name": "entity_name",
    "entity legal name": "entity_name",
    "legal name": "entity_name",
    "investor name": "entity_name",
    "name": "entity_name",
    "entity type": "entity_type",
    "type": "entity_type",
    "primary email": "primary_email",
    "email": "primary_email",
    "client email": "primary_email",
    "tax id": "tax_id",
    "tax id number": "tax_id",
    "ssn": "tax_id",
    "ein": "tax_id",
    "tax id type": "tax_id_type",
    "mailing address": "mailing_address",
    "address": "mailing_address",
    "phone": "phone",
    "phone number": "phone",
    "advisor email": "advisor_email",
    "advisor": "advisor_email",
    "date of birth": "date_of_birth",
    "dob": "date_of_birth",
    "country of formation": "country_of_formation",
    "state of formation": "state_of_formation",
    "orion id": "orion_id",
    "orion household": "orion_household_name",
    "household name": "orion_household_name",
}

# Optional commitment columns (only used when import_type=investors_and_commitments)
COMMITMENT_COLUMN_MAP = {
    "committed amount": "committed_amount",
    "commitment amount": "committed_amount",
    "commitment": "committed_amount",
    "funded amount": "funded_amount",
    "fee amount": "fee_amount",
    "advisory fee pct": "advisory_fee_pct",
    "advisory fee %": "advisory_fee_pct",
    "memorandum number": "memorandum_number",
    "memo number": "memorandum_number",
    "memo #": "memorandum_number",
}


def _normalize_header(h: str) -> str:
    return (h or "").strip().lower().replace("_", " ").replace("-", " ")


def _parse_csv_upload(file_bytes: bytes) -> tuple[list[str], list[dict]]:
    """Return (detected_headers, parsed_rows). Rows are dicts keyed by raw header."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = list(reader)
    return headers, rows


def _map_row_to_investor(row: dict, mapping: dict[str, str]) -> dict:
    """Apply column mapping → investors columns. Returns dict of mapped fields."""
    out: dict = {}
    for raw_header, value in row.items():
        col = mapping.get(_normalize_header(raw_header))
        if not col or value is None or str(value).strip() == "":
            continue
        out[col] = str(value).strip()
    return out


def _map_row_to_commitment(row: dict) -> dict:
    """Extract any commitment fields present in this row."""
    out: dict = {}
    for raw_header, value in row.items():
        col = COMMITMENT_COLUMN_MAP.get(_normalize_header(raw_header))
        if not col or value is None or str(value).strip() == "":
            continue
        raw = str(value).strip().replace("$", "").replace(",", "")
        try:
            out[col] = float(raw) if col in ("committed_amount", "funded_amount", "fee_amount", "advisory_fee_pct") else raw
        except ValueError:
            continue
    return out


@router.post("/import/csv")
async def import_csv(
    file: UploadFile = File(...),
    import_type: str = Form(default="investors_only"),  # investors_only | investors_and_commitments
    target_deal_id: Optional[str] = Form(default=None),
    dry_run: bool = Form(default=True),
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Import investors (and optionally commitments) from a CSV file.

    Two-phase flow:
      1. Upload with dry_run=true  → returns parsed preview, detected mappings,
         and validation errors. Nothing is written.
      2. Upload again with dry_run=false → commits the import.

    Required headers (any of these variants accepted):
      Entity Name | Entity Legal Name | Legal Name | Investor Name

    Optional headers map to: entity_type, primary_email, tax_id, mailing_address,
      phone, advisor_email, date_of_birth, country_of_formation, state_of_formation,
      orion_id, orion_household_name.

    When import_type=investors_and_commitments, also accepts:
      Committed Amount, Funded Amount, Fee Amount, Advisory Fee %, Memo Number
      Requires target_deal_id (the destination deal — typically a closed deal in
      the Fund Ledger when migrating historical data).
    """
    firm_id = _require_firm(x_firm_id)

    if import_type not in ("investors_only", "investors_and_commitments"):
        raise HTTPException(status_code=400, detail="import_type must be investors_only or investors_and_commitments")

    if import_type == "investors_and_commitments" and not target_deal_id:
        raise HTTPException(status_code=400, detail="target_deal_id is required when importing commitments")

    if target_deal_id:
        deal = (
            supabase.table("deals")
            .select("id, offering_name, status")
            .eq("id", target_deal_id)
            .eq("firm_id", firm_id)
            .single()
            .execute()
            .data
        )
        if not deal:
            raise HTTPException(status_code=404, detail="target_deal_id not found in this firm")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file upload.")

    headers, rows = _parse_csv_upload(file_bytes)
    if not headers:
        raise HTTPException(status_code=400, detail="Could not detect any CSV headers.")

    # Build the active mapping (only headers we recognize)
    detected_mapping: dict[str, str] = {}
    for h in headers:
        col = INVESTOR_COLUMN_MAP.get(_normalize_header(h))
        if col:
            detected_mapping[h] = col

    if "entity_name" not in detected_mapping.values():
        raise HTTPException(
            status_code=400,
            detail="CSV must include an entity name column. Accepted headers: Entity Name, Entity Legal Name, Legal Name, Investor Name.",
        )

    # Build the import plan
    plan: list[dict] = []
    errors: list[dict] = []
    for idx, row in enumerate(rows, start=2):  # row 1 = header
        investor_fields = _map_row_to_investor(row, {_normalize_header(k): v for k, v in INVESTOR_COLUMN_MAP.items()})
        if not investor_fields.get("entity_name"):
            errors.append({"row": idx, "error": "missing entity_name"})
            continue
        investor_fields["firm_id"] = firm_id
        investor_fields.setdefault("kyc_status", "Pending")

        record = {"row": idx, "investor": investor_fields}
        if import_type == "investors_and_commitments":
            commitment_fields = _map_row_to_commitment(row)
            if not commitment_fields.get("committed_amount"):
                errors.append({"row": idx, "error": "missing committed_amount for commitment import"})
                continue
            commitment_fields["firm_id"] = firm_id
            commitment_fields["deal_id"] = target_deal_id
            commitment_fields.setdefault("status", "Active")
            record["commitment"] = commitment_fields
        plan.append(record)

    response = {
        "dry_run": dry_run,
        "detected_headers": headers,
        "detected_mapping": detected_mapping,
        "import_type": import_type,
        "target_deal_id": target_deal_id,
        "total_rows": len(rows),
        "rows_to_import": len(plan),
        "errors": errors,
        "preview": plan[:5],
    }

    if dry_run:
        return response

    # Commit phase
    imported_investors = 0
    imported_commitments = 0
    commit_errors: list[dict] = []

    for record in plan:
        try:
            inv_result = (
                supabase.table("investors")
                .upsert(record["investor"], on_conflict="entity_name")
                .execute()
                .data
            )
            if not inv_result:
                commit_errors.append({"row": record["row"], "error": "investor upsert returned no data"})
                continue
            investor_id = inv_result[0]["id"]
            imported_investors += 1

            if "commitment" in record:
                commitment_data = {**record["commitment"], "investor_id": investor_id}
                supabase.table("commitments").insert(commitment_data).execute()
                imported_commitments += 1
        except Exception as e:
            commit_errors.append({"row": record["row"], "error": str(e)})

    log_audit(
        firm_id=firm_id,
        actor_type="ops",
        actor_id=None,
        entity_type="firm",
        entity_id=firm_id,
        action="csv_import_committed",
        diff={
            "import_type": import_type,
            "target_deal_id": target_deal_id,
            "imported_investors": imported_investors,
            "imported_commitments": imported_commitments,
            "errors": len(commit_errors),
        },
    )

    response.update({
        "imported_investors": imported_investors,
        "imported_commitments": imported_commitments,
        "commit_errors": commit_errors,
    })
    return response
