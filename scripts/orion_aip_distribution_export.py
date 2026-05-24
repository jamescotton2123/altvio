"""
Orion AIP Distribution Import Generator.

SCAFFOLD STATUS: Column mappings are stubbed with TODO markers.
This script is BLOCKED pending Orion AIP template files from the firm.
Once templates are provided, replace the TODO column names with the exact
headers Orion expects. The scaffolding, validation logic, and file I/O
are fully implemented and ready to wire up.

What it produces:
  - AIPTransaction_{YYYY-MM-DD}.csv  — transaction-level distribution records
  - AIPAsset_{YYYY-MM-DD}.csv        — asset/position-level records

TPA validation:
  - Ops enters the TPA/accounting-confirmed total when initiating.
  - The script sums all individual amounts and compares with $0.01 tolerance.
  - If totals don't match, the export is blocked with a clear error.
  - This prevents sending wrong numbers to Orion.

Fee mode (controlled by firm_settings):
  - "embedded"  — commitment + management fee in a single row
  - "separate"  — one row for the distribution, one row for the management fee
"""

import csv
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# Allow running directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import supabase

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"
TOLERANCE = Decimal("0.01")


# ---------------------------------------------------------------------------
# TODO: Replace stub column names with exact Orion AIP template headers.
# These will be confirmed once Orion AIP template files are provided.
# ---------------------------------------------------------------------------

AIP_TRANSACTION_COLUMNS = [
    # TODO: confirm exact header names from Orion AIPTransaction template
    "AccountNumber",      # Orion account/household identifier
    "TransactionDate",    # Date of distribution
    "TransactionType",    # Type code from firm_settings (e.g. 'DIST', 'DISSOLUTION')
    "Amount",             # Distribution amount for this investor
    "Description",        # Free-text description / memo
    "FundName",           # Fund/deal name
    "InvestorName",       # Investor entity name
    # TODO: add any additional required columns from the template
]

AIP_ASSET_COLUMNS = [
    # TODO: confirm exact header names from Orion AIPAsset template
    "AccountNumber",      # Orion account/household identifier
    "AssetID",            # Orion asset ID (from investors.orion_asset_id)
    "AssetDate",          # Date of asset record
    "Quantity",           # TODO: confirm semantics — units, NAV, or dollar value?
    "Price",              # TODO: confirm if price per unit is required
    "MarketValue",        # Total market value / distribution amount
    "Description",        # Free-text description
    # TODO: add any additional required columns from the template
]


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_firm_settings(firm_id: str) -> dict:
    result = (
        supabase.table("firm_settings")
        .select("orion_aip_distribution_type_code, orion_aip_dissolution_type_code, orion_fee_mode")
        .eq("firm_id", firm_id)
        .single()
        .execute()
    )
    if not result.data:
        raise ValueError(f"Firm settings not found for firm_id={firm_id}")
    return result.data


def _load_distribution_notices(distribution_id: str, firm_id: str) -> list[dict]:
    """Load all distribution notices with investor and deal details."""
    result = (
        supabase.table("distribution_notices")
        .select(
            "individual_amount, "
            "investors(entity_name, orion_id, orion_account_number, orion_asset_id, orion_household_name), "
            "distributions(distribution_date, distribution_type, deals(offering_name))"
        )
        .eq("distribution_id", distribution_id)
        .eq("firm_id", firm_id)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _build_transaction_row(
    notice: dict,
    type_code: str,
    is_fee_row: bool = False,
) -> dict:
    """Build a single AIPTransaction row from a distribution notice."""
    investor = notice.get("investors", {})
    distribution = notice.get("distributions", {})
    deal = distribution.get("deals", {})
    amount = float(notice.get("individual_amount", 0))

    return {
        "AccountNumber": investor.get("orion_account_number") or investor.get("orion_id") or "",
        "TransactionDate": str(distribution.get("distribution_date", date.today())),
        "TransactionType": type_code,
        "Amount": f"{amount:.2f}",
        "Description": f"{'Fee' if is_fee_row else 'Distribution'} — {deal.get('offering_name', '')}",
        "FundName": deal.get("offering_name", ""),
        "InvestorName": investor.get("entity_name", ""),
        # TODO: populate additional columns once template is confirmed
    }


def _build_asset_row(notice: dict) -> dict:
    """Build a single AIPAsset row from a distribution notice."""
    investor = notice.get("investors", {})
    distribution = notice.get("distributions", {})
    deal = distribution.get("deals", {})
    amount = float(notice.get("individual_amount", 0))

    return {
        "AccountNumber": investor.get("orion_account_number") or investor.get("orion_id") or "",
        "AssetID": investor.get("orion_asset_id") or "",
        "AssetDate": str(distribution.get("distribution_date", date.today())),
        "Quantity": "1",       # TODO: confirm with Orion whether this should be units or dollar value
        "Price": f"{amount:.2f}",
        "MarketValue": f"{amount:.2f}",
        "Description": f"Distribution — {deal.get('offering_name', '')}",
        # TODO: populate additional columns once template is confirmed
    }


# ---------------------------------------------------------------------------
# TPA validation
# ---------------------------------------------------------------------------

def _validate_total(notices: list[dict], tpa_confirmed_total: float) -> tuple[float, bool]:
    """
    Sum all individual distribution amounts and compare to TPA-confirmed total.
    Returns (calculated_total, is_valid).
    Tolerance: $0.01 to account for rounding across many rows.
    """
    total = sum(float(n.get("individual_amount", 0)) for n in notices)
    delta = abs(total - tpa_confirmed_total)
    is_valid = delta <= 0.01
    return total, is_valid


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------

def _write_csv(rows: list[dict], columns: list[str], filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"[OrionAIP] Written: {filepath} ({len(rows)} row(s))")


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def generate_distribution_aip(
    distribution_id: str,
    firm_id: str,
    tpa_confirmed_total: float,
    is_dissolution: bool = False,
) -> dict:
    """
    Generate Orion AIP import files for a distribution or dissolution.

    Args:
        distribution_id: UUID of the distribution record.
        firm_id: UUID of the firm.
        tpa_confirmed_total: The total dollar amount confirmed by accounting/TPA.
                             Must match the sum of individual amounts within $0.01.
        is_dissolution: If True, uses the dissolution type code instead of distribution.

    Returns:
        {
            "aip_transaction": str,   # path to AIPTransaction file
            "aip_asset": str,         # path to AIPAsset file
            "total_validated": bool,
            "calculated_total": float,
            "tpa_total": float,
            "row_count": int,
        }

    Raises:
        ValueError if TPA total doesn't match within tolerance.
    """
    settings = _load_firm_settings(firm_id)
    notices = _load_distribution_notices(distribution_id, firm_id)

    if not notices:
        raise ValueError(f"No distribution notices found for distribution_id={distribution_id}")

    # TPA validation — must pass before any files are written
    calculated_total, is_valid = _validate_total(notices, tpa_confirmed_total)
    if not is_valid:
        delta = abs(calculated_total - tpa_confirmed_total)
        raise ValueError(
            f"TPA validation FAILED: calculated ${calculated_total:,.2f} vs. "
            f"TPA-confirmed ${tpa_confirmed_total:,.2f} (delta: ${delta:,.2f}). "
            "Correct the amounts before generating Orion files."
        )

    fee_mode = settings.get("orion_fee_mode", "embedded")
    type_code = (
        settings.get("orion_aip_dissolution_type_code", "DISSOLUTION")
        if is_dissolution
        else settings.get("orion_aip_distribution_type_code", "DIST")
    )

    today = date.today().isoformat()
    output_dir = EXPORTS_DIR / firm_id

    # Build transaction rows
    transaction_rows = []
    for notice in notices:
        transaction_rows.append(_build_transaction_row(notice, type_code, is_fee_row=False))
        if fee_mode == "separate":
            # TODO: Add separate fee row logic once fee calculation is confirmed
            # This would add a second row per investor for the management fee
            pass

    # Build asset rows
    asset_rows = [_build_asset_row(n) for n in notices]

    # Write files
    txn_path = output_dir / f"AIPTransaction_{today}.csv"
    asset_path = output_dir / f"AIPAsset_{today}.csv"

    _write_csv(transaction_rows, AIP_TRANSACTION_COLUMNS, txn_path)
    _write_csv(asset_rows, AIP_ASSET_COLUMNS, asset_path)

    print(f"[OrionAIP] Export complete — {len(transaction_rows)} investors, total: ${calculated_total:,.2f}")

    return {
        "aip_transaction": str(txn_path),
        "aip_asset": str(asset_path),
        "total_validated": is_valid,
        "calculated_total": calculated_total,
        "tpa_total": tpa_confirmed_total,
        "row_count": len(notices),
    }


# ---------------------------------------------------------------------------
# CLI entry point for manual runs
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate Orion AIP distribution import files.")
    parser.add_argument("--distribution-id", required=True, help="UUID of the distribution record.")
    parser.add_argument("--firm-id", required=True, help="UUID of the firm.")
    parser.add_argument("--tpa-total", required=True, type=float, help="TPA/accounting-confirmed distribution total.")
    parser.add_argument("--dissolution", action="store_true", help="Use dissolution type code instead of distribution.")
    args = parser.parse_args()

    result = generate_distribution_aip(
        distribution_id=args.distribution_id,
        firm_id=args.firm_id,
        tpa_confirmed_total=args.tpa_total,
        is_dissolution=args.dissolution,
    )
    print("\n=== Orion AIP Export Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
