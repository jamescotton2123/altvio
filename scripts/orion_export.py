"""
Orion NAImport ETL export script.
Generates two CSVs for a given deal:
  - NAImport_New_{date}.csv   — full records for investors with no orion_id
  - NAImport_Existing_{date}.csv — lightweight records for investors with confirmed orion_id

Fee row behavior is driven by firm_settings.orion_aip_fee_mode:
  'separate_rows' — commitment and fee each get their own AIP transaction row
  'embedded'      — fee is baked into the commitment amount (single row)

IMPORTANT: Script refuses to run if any investor in the deal has
orion_match_status = 'Needs Review' or 'No Match Found'.
This prevents bad household names from shipping to Orion.
"""

import sys
import os
import csv
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.database import supabase

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"


def get_deal_commitments(deal_id: str, firm_id: str) -> list[dict]:
    return (
        supabase.table("commitments")
        .select("id, committed_amount, fee_amount, status, advisory_fee_pct, investors(*), deals(offering_name)")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    )


def validate_no_pending_matches(commitments: list[dict]) -> None:
    """
    Raise ValueError if any existing investor (without orion_is_new_household flag)
    has an unresolved Orion match. Investors with orion_is_new_household=True
    bypass this check entirely — they always go to NAImport_New.
    Raises ValueError instead of sys.exit so callers can handle gracefully.
    """
    blocked = []
    for c in commitments:
        investor = c.get("investors", {})
        # Skip investors explicitly flagged as new households
        if investor.get("orion_is_new_household"):
            continue
        if investor.get("orion_id") and investor.get("orion_match_status") not in ("Confirmed",):
            blocked.append(investor.get("entity_name"))

    if blocked:
        names = ", ".join(blocked)
        raise ValueError(
            f"Unresolved Orion household matches for: {names}. "
            "Resolve these in the Deal Hub Orion Review Queue before exporting."
        )


def calculate_fee(committed_amount: float, settings: dict) -> float:
    fee_basis = settings.get("orion_fee_basis", "percent_committed")
    fee_rate = float(settings.get("orion_fee_rate") or 0)

    if fee_basis == "percent_committed":
        return round(committed_amount * (fee_rate / 100), 2)
    elif fee_basis == "flat":
        return round(fee_rate, 2)
    return 0.0


def write_naimport_new(commitments: list[dict], firm_slug: str, offering_name: str, settings: dict) -> Path:
    """Export full investor records for new investors (orion_id IS NULL)."""
    output_dir = EXPORTS_DIR / firm_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"NAImport_New_{date.today().isoformat()}.csv"

    fee_mode = settings.get("orion_aip_fee_mode", "separate_rows")
    fee_type_code = settings.get("orion_aip_fee_type_code", "MGMT_FEE")
    commitment_type_code = settings.get("orion_aip_commitment_type_code", "COMMITMENT")

    # New investors = no orion_id OR explicitly flagged as new household (bypasses fuzzy matcher)
    new_investors = [
        c for c in commitments
        if not c.get("investors", {}).get("orion_id")
        or c.get("investors", {}).get("orion_is_new_household")
    ]
    if not new_investors:
        print("[OrionExport] No new investors to export.")
        return None

    rows = []
    for c in new_investors:
        inv = c.get("investors", {})
        committed = float(c.get("committed_amount") or 0)
        fee = calculate_fee(committed, settings)

        base_row = {
            "EntityName": inv.get("entity_name", ""),
            "EntityType": inv.get("entity_type", ""),
            "TaxID": inv.get("tax_id", ""),
            "PrimaryEmail": inv.get("primary_email", ""),
            "MailingAddress": inv.get("mailing_address", ""),
            "FundName": offering_name,
            "CommitmentAmount": committed if fee_mode == "separate_rows" else round(committed + fee, 2),
            "TransactionTypeCode": commitment_type_code,
            "AdvisorEmail": inv.get("advisor_email", ""),
        }
        rows.append(base_row)

        if fee_mode == "separate_rows" and fee > 0:
            fee_row = {**base_row, "CommitmentAmount": fee, "TransactionTypeCode": fee_type_code}
            rows.append(fee_row)

    fieldnames = list(rows[0].keys()) if rows else []
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OrionExport] NAImport (New) written: {filename} ({len(new_investors)} investor(s))")
    return filename


def write_naimport_existing(commitments: list[dict], firm_slug: str, offering_name: str, settings: dict) -> Path:
    """Export lightweight records for existing investors (orion_id IS NOT NULL + Confirmed)."""
    output_dir = EXPORTS_DIR / firm_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"NAImport_Existing_{date.today().isoformat()}.csv"

    fee_mode = settings.get("orion_aip_fee_mode", "separate_rows")
    fee_type_code = settings.get("orion_aip_fee_type_code", "MGMT_FEE")
    commitment_type_code = settings.get("orion_aip_commitment_type_code", "COMMITMENT")

    # Existing investors = have confirmed orion_id AND NOT flagged as new household
    existing = [
        c for c in commitments
        if c.get("investors", {}).get("orion_id")
        and c.get("investors", {}).get("orion_match_status") == "Confirmed"
        and not c.get("investors", {}).get("orion_is_new_household")
    ]
    if not existing:
        print("[OrionExport] No existing investors to export.")
        return None

    rows = []
    for c in existing:
        inv = c.get("investors", {})
        committed = float(c.get("committed_amount") or 0)
        fee = calculate_fee(committed, settings)

        base_row = {
            "OrionID": inv.get("orion_id", ""),
            "HouseholdName": inv.get("orion_household_name", ""),
            "FundName": offering_name,
            "CommitmentAmount": committed if fee_mode == "separate_rows" else round(committed + fee, 2),
            "TransactionTypeCode": commitment_type_code,
        }
        rows.append(base_row)

        if fee_mode == "separate_rows" and fee > 0:
            fee_row = {**base_row, "CommitmentAmount": fee, "TransactionTypeCode": fee_type_code}
            rows.append(fee_row)

    fieldnames = list(rows[0].keys()) if rows else []
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OrionExport] NAImport (Existing) written: {filename} ({len(existing)} investor(s))")

    # After export, new investors get their orion_id placeholder flagged for ops to populate
    for c in existing:
        inv = c.get("investors", {})
        if not inv.get("orion_id"):
            supabase.table("investors").update({
                "orion_match_status": "Exported — Awaiting Orion ID",
            }).eq("id", inv["id"]).execute()

    return filename


def run_naimport_export(deal_id: str, firm_id: str) -> dict:
    """Main entry point. Run both NAImport exports for a deal."""
    settings = (
        supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute().data
    )
    firm = supabase.table("firms").select("name, slug").eq("id", firm_id).single().execute().data

    commitments = get_deal_commitments(deal_id, firm_id)
    if not commitments:
        print("[OrionExport] No active commitments found for this deal.")
        return {}

    offering_name = commitments[0].get("deals", {}).get("offering_name", "Unknown Fund")
    firm_slug = firm.get("slug", firm_id)

    validate_no_pending_matches(commitments)

    new_file = write_naimport_new(commitments, firm_slug, offering_name, settings)
    existing_file = write_naimport_existing(commitments, firm_slug, offering_name, settings)

    return {
        "naimport_new": str(new_file) if new_file else None,
        "naimport_existing": str(existing_file) if existing_file else None,
        "offering_name": offering_name,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Orion NAImport files for a deal.")
    parser.add_argument("--deal-id", required=True, help="Deal UUID")
    parser.add_argument("--firm-id", required=True, help="Firm UUID")
    args = parser.parse_args()

    try:
        result = run_naimport_export(deal_id=args.deal_id, firm_id=args.firm_id)
        print("\nExport complete:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    except ValueError as e:
        print(f"\n[OrionExport] BLOCKED: {e}")
        sys.exit(1)
