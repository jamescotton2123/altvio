"""
Reconcile annual AIP bps billing against funded capital.

Outputs firms whose billed AIP fees exceed 1.5 bps of funded capital for the year.
"""

import argparse
import csv
import sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.billing import AIP_BPS_ANNUAL
from core.database import supabase

COLUMNS = ["firm_id", "expected_cents", "billed_cents", "overcharge_cents"]
AIP_EVENT_TYPES = {"aip_bps_quarterly", "aip_bps_monthly"}


def _money_cents(amount: Decimal) -> int:
    return int((amount * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _is_period_in_year(billing_period: str, year: int) -> bool:
    return billing_period.startswith(f"{year}-") or billing_period.startswith(f"{year}-Q")


def _load_funded_by_firm() -> dict[str, Decimal]:
    rows = (
        supabase.table("commitments")
        .select("firm_id, funded_amount")
        .execute()
        .data
        or []
    )
    funded_by_firm: dict[str, Decimal] = {}
    for row in rows:
        firm_id = row.get("firm_id")
        if not firm_id:
            continue
        funded_by_firm.setdefault(firm_id, Decimal("0"))
        funded_by_firm[firm_id] += Decimal(str(row.get("funded_amount") or 0))
    return funded_by_firm


def _load_billed_aip_by_firm(year: int) -> dict[str, int]:
    rows = (
        supabase.table("billing_usage")
        .select("firm_id, event_type, amount_cents, billing_period")
        .execute()
        .data
        or []
    )
    billed_by_firm: dict[str, int] = {}
    for row in rows:
        firm_id = row.get("firm_id")
        billing_period = str(row.get("billing_period") or "")
        if not firm_id:
            continue
        if row.get("event_type") not in AIP_EVENT_TYPES:
            continue
        if not _is_period_in_year(billing_period, year):
            continue
        billed_by_firm.setdefault(firm_id, 0)
        billed_by_firm[firm_id] += int(row.get("amount_cents") or 0)
    return billed_by_firm


def reconcile_aip_billing(year: int) -> list[dict[str, int | str]]:
    funded_by_firm = _load_funded_by_firm()
    billed_by_firm = _load_billed_aip_by_firm(year)

    rows: list[dict[str, int | str]] = []
    for firm_id, billed_cents in sorted(billed_by_firm.items()):
        expected_cents = _money_cents(funded_by_firm.get(firm_id, Decimal("0")) * AIP_BPS_ANNUAL)
        if billed_cents <= expected_cents:
            continue
        rows.append({
            "firm_id": firm_id,
            "expected_cents": expected_cents,
            "billed_cents": billed_cents,
            "overcharge_cents": billed_cents - expected_cents,
        })
    return rows


def write_reconciliation_csv(rows: list[dict[str, int | str]], output_path: Path | None) -> None:
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[BillingReconciliation] Written: {output_path} ({len(rows)} row(s))")
        return

    writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile annual AIP bps billing overcharges.")
    parser.add_argument("--year", type=int, default=date.today().year, help="Billing year to reconcile.")
    parser.add_argument("--output", type=Path, help="Optional CSV output path. Defaults to stdout.")
    args = parser.parse_args()

    write_reconciliation_csv(reconcile_aip_billing(args.year), args.output)
