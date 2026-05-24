"""
Wire Upload Template Generator.
Produces bank-formatted CSV files for:
  - Distribution wires (fund → investors, outbound)
  - Capital call wires (investors → fund, inbound request)

Bank adapter pattern: firm_settings.wire_bank selects the formatter.
Each adapter maps the same internal WireRecord to that bank's exact CSV column spec.
New banks are added by writing a new adapter class — no changes to core logic.

IMPORTANT: Direct bank API submission is not supported by boutique RIA firms.
These files are downloaded by ops and uploaded to the bank's online portal.
The manual step is a single file upload — under 30 seconds.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"
logger = logging.getLogger(__name__)


@dataclass
class WireRecord:
    """Internal wire record — bank-agnostic. All adapters consume this."""
    beneficiary_name: str
    beneficiary_account: str
    routing_number: str
    bank_name: str
    amount: float
    reference_memo: str
    wire_date: str
    beneficiary_address: Optional[str] = None
    bank_address: Optional[str] = None
    intermediary_bank: Optional[str] = None
    intermediary_routing: Optional[str] = None
    wire_type: str = "domestic"  # domestic | international


# ---------------------------------------------------------------------------
# Bank Adapters
# ---------------------------------------------------------------------------

class GenericAdapter:
    """
    Generic NACHA-style fallback. Use when bank is unknown or not yet mapped.
    Covers the standard fields accepted by most online banking portals.
    """
    COLUMNS = [
        "WireDate", "BeneficiaryName", "BeneficiaryAccount",
        "RoutingNumber", "BankName", "Amount", "Memo",
        "BeneficiaryAddress", "WireType",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "WireDate": record.wire_date,
            "BeneficiaryName": record.beneficiary_name,
            "BeneficiaryAccount": record.beneficiary_account,
            "RoutingNumber": record.routing_number,
            "BankName": record.bank_name,
            "Amount": f"{record.amount:.2f}",
            "Memo": record.reference_memo,
            "BeneficiaryAddress": record.beneficiary_address or "",
            "WireType": record.wire_type,
        }


class JPMorganAdapter:
    """
    JPMorgan Chase wire upload format.
    Compatible with JPMorgan Access and Chase Business Online batch wire upload.
    """
    COLUMNS = [
        "Payment Date", "Debit Account", "Beneficiary Name",
        "Beneficiary Account Number", "Beneficiary Bank ABA",
        "Beneficiary Bank Name", "Payment Amount", "Payment Currency",
        "Reference for Beneficiary", "Originator to Beneficiary Info",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "Payment Date": record.wire_date,
            "Debit Account": "",  # Filled by ops — firm's account number
            "Beneficiary Name": record.beneficiary_name,
            "Beneficiary Account Number": record.beneficiary_account,
            "Beneficiary Bank ABA": record.routing_number,
            "Beneficiary Bank Name": record.bank_name,
            "Payment Amount": f"{record.amount:.2f}",
            "Payment Currency": "USD",
            "Reference for Beneficiary": record.reference_memo,
            "Originator to Beneficiary Info": record.reference_memo,
        }


class SchwabAdapter:
    """
    Charles Schwab Advisor Services wire upload format.
    Compatible with Schwab Alliance batch wire import.
    """
    COLUMNS = [
        "Trade Date", "Account Number", "Recipient Name",
        "Recipient Account", "ABA Routing Number", "Bank Name",
        "Wire Amount", "Wire Memo", "Address Line 1",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "Trade Date": record.wire_date,
            "Account Number": "",  # Ops fills — client account at Schwab
            "Recipient Name": record.beneficiary_name,
            "Recipient Account": record.beneficiary_account,
            "ABA Routing Number": record.routing_number,
            "Bank Name": record.bank_name,
            "Wire Amount": f"{record.amount:.2f}",
            "Wire Memo": record.reference_memo,
            "Address Line 1": record.beneficiary_address or "",
        }


class PershingAdapter:
    """
    Pershing (BNY Mellon) wire upload format.
    Compatible with NetX360 batch wire processing.
    """
    COLUMNS = [
        "Settlement Date", "Beneficiary Name", "Beneficiary Acct#",
        "Receiving Bank ABA", "Receiving Bank Name", "Wire Amount",
        "OBI Field 1", "OBI Field 2", "Intermediary Bank ABA",
        "Intermediary Bank Name",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "Settlement Date": record.wire_date,
            "Beneficiary Name": record.beneficiary_name,
            "Beneficiary Acct#": record.beneficiary_account,
            "Receiving Bank ABA": record.routing_number,
            "Receiving Bank Name": record.bank_name,
            "Wire Amount": f"{record.amount:.2f}",
            "OBI Field 1": record.reference_memo,
            "OBI Field 2": "",
            "Intermediary Bank ABA": record.intermediary_routing or "",
            "Intermediary Bank Name": record.intermediary_bank or "",
        }


class FidelityAdapter:
    """
    Fidelity Institutional wire upload format.
    Compatible with Fidelity WealthCentral batch wire entry.
    """
    COLUMNS = [
        "Effective Date", "Payee Name", "Payee Account Number",
        "Payee Bank Routing", "Payee Bank Name", "Dollar Amount",
        "Payment Reference", "Payee Address",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "Effective Date": record.wire_date,
            "Payee Name": record.beneficiary_name,
            "Payee Account Number": record.beneficiary_account,
            "Payee Bank Routing": record.routing_number,
            "Payee Bank Name": record.bank_name,
            "Dollar Amount": f"{record.amount:.2f}",
            "Payment Reference": record.reference_memo,
            "Payee Address": record.beneficiary_address or "",
        }


class WellsFargoAdapter:
    """
    Wells Fargo Commercial Electronic Office (CEO) wire upload format.
    Compatible with Wells Fargo CEO batch wire import.
    """
    COLUMNS = [
        "Value Date", "Beneficiary Name", "Beneficiary Account",
        "Beneficiary Bank Routing", "Beneficiary Bank", "Amount",
        "Currency", "Payment Details", "Beneficiary Address",
        "Bank Address",
    ]

    @classmethod
    def format(cls, record: WireRecord) -> dict:
        return {
            "Value Date": record.wire_date,
            "Beneficiary Name": record.beneficiary_name,
            "Beneficiary Account": record.beneficiary_account,
            "Beneficiary Bank Routing": record.routing_number,
            "Beneficiary Bank": record.bank_name,
            "Amount": f"{record.amount:.2f}",
            "Currency": "USD",
            "Payment Details": record.reference_memo,
            "Beneficiary Address": record.beneficiary_address or "",
            "Bank Address": record.bank_address or "",
        }


class CustomAdapter:
    """
    Firm-specific custom bank adapter.
    Activated when firm_settings.wire_bank == 'custom'.
    Column spec is fully driven by firm_settings.custom_wire_column_map —
    no hardcoded columns. The mapping is learned by core.bank_template_learner.

    column_map format:
      { "wire_date": "Settlement Date", "amount": "Wire Amount USD", ... }

    Field values are pulled from the WireRecord by the matching internal field name.
    """

    def __init__(self, column_map: dict):
        self.column_map = column_map
        self.COLUMNS = list(column_map.values())

    def format(self, record: WireRecord) -> dict:
        internal_values = {
            "wire_date": record.wire_date,
            "beneficiary_name": record.beneficiary_name,
            "beneficiary_account": record.beneficiary_account,
            "routing_number": record.routing_number,
            "bank_name": record.bank_name,
            "amount": f"{record.amount:.2f}",
            "reference_memo": record.reference_memo,
            "beneficiary_address": record.beneficiary_address or "",
            "bank_address": record.bank_address or "",
            "intermediary_bank": record.intermediary_bank or "",
            "intermediary_routing": record.intermediary_routing or "",
        }
        return {
            csv_col: internal_values.get(internal_field, "")
            for internal_field, csv_col in self.column_map.items()
        }


BANK_ADAPTERS = {
    "jpmorgan": JPMorganAdapter,
    "schwab": SchwabAdapter,
    "pershing": PershingAdapter,
    "fidelity": FidelityAdapter,
    "wells_fargo": WellsFargoAdapter,
    "generic": GenericAdapter,
    # 'custom' is handled dynamically in _write_wire_file
}


# ---------------------------------------------------------------------------
# Wire record builders
# ---------------------------------------------------------------------------

def _parse_wire_instructions(raw: str) -> dict:
    """
    Parse free-text wire instructions stored in investor/deals records.
    Extracts routing number, account number, bank name, and address heuristically.
    Falls back to empty strings if a field can't be parsed — ops can correct in the file.
    """
    import re
    result = {
        "routing_number": "",
        "account_number": "",
        "bank_name": "",
        "bank_address": "",
    }

    routing_match = re.search(r"(?:ABA|Routing|RTN)[:\s#]*([0-9]{9})", raw, re.IGNORECASE)
    if routing_match:
        result["routing_number"] = routing_match.group(1)

    account_match = re.search(r"(?:Account|Acct)[:\s#]*([0-9A-Za-z\-]+)", raw, re.IGNORECASE)
    if account_match:
        result["account_number"] = account_match.group(1)

    bank_match = re.search(r"(?:Bank|FBO)[:\s]+([^\n,]+)", raw, re.IGNORECASE)
    if bank_match:
        result["bank_name"] = bank_match.group(1).strip()

    return result


def _build_wire_records_for_distribution(distribution_id: str, firm_id: str) -> list[WireRecord]:
    """Build wire records for a distribution (fund → investors)."""
    from core.database import supabase

    distribution = (
        supabase.table("distributions")
        .select("distribution_date, total_amount, distribution_type, deals(offering_name, wire_instructions)")
        .eq("id", distribution_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not distribution:
        raise ValueError(f"Distribution {distribution_id} not found.")

    notices = (
        supabase.table("distribution_notices")
        .select("individual_amount, investors(entity_name, mailing_address, primary_email)")
        .eq("distribution_id", distribution_id)
        .eq("firm_id", firm_id)
        .eq("status", "Sent")
        .execute()
        .data
    )

    offering_name = distribution.get("deals", {}).get("offering_name", "Fund")
    dist_date = str(distribution.get("distribution_date", date.today()))

    records = []
    for notice in notices:
        investor = notice.get("investors", {})
        amount = float(notice.get("individual_amount", 0))
        if amount <= 0:
            continue

        # Wire instructions for distributions come from the fund's bank (deals table)
        raw_wire = distribution.get("deals", {}).get("wire_instructions", "")
        parsed = _parse_wire_instructions(raw_wire)

        records.append(WireRecord(
            beneficiary_name=investor.get("entity_name", ""),
            beneficiary_account=parsed["account_number"],
            routing_number=parsed["routing_number"],
            bank_name=parsed["bank_name"],
            amount=amount,
            reference_memo=f"{offering_name} Distribution",
            wire_date=dist_date,
            beneficiary_address=investor.get("mailing_address"),
        ))

    return records


def _build_wire_records_for_capital_call(capital_call_id: str, firm_id: str) -> list[WireRecord]:
    """Build wire records for a capital call (investors → fund, inbound)."""
    from core.database import supabase

    capital_call = (
        supabase.table("capital_calls")
        .select("call_date, call_type, deals(offering_name, wire_instructions)")
        .eq("id", capital_call_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not capital_call:
        raise ValueError(f"Capital call {capital_call_id} not found.")

    notices = (
        supabase.table("capital_call_notices")
        .select("individual_amount, due_date, investors(entity_name, mailing_address)")
        .eq("capital_call_id", capital_call_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )

    offering_name = capital_call.get("deals", {}).get("offering_name", "Fund")
    call_date = str(capital_call.get("call_date", date.today()))

    # For capital calls, the wire destination is the FUND's bank account (stored in deals)
    raw_wire = capital_call.get("deals", {}).get("wire_instructions", "")
    parsed = _parse_wire_instructions(raw_wire)

    records = []
    for notice in notices:
        investor = notice.get("investors", {})
        amount = float(notice.get("individual_amount", 0))
        if amount <= 0:
            continue

        records.append(WireRecord(
            beneficiary_name=offering_name,
            beneficiary_account=parsed["account_number"],
            routing_number=parsed["routing_number"],
            bank_name=parsed["bank_name"],
            amount=amount,
            reference_memo=f"{investor.get('entity_name', '')} — {offering_name} Capital Call",
            wire_date=str(notice.get("due_date") or call_date),
            beneficiary_address=None,
        ))

    return records


# ---------------------------------------------------------------------------
# Main export functions
# ---------------------------------------------------------------------------

def generate_distribution_wire_file(
    distribution_id: str,
    firm_id: str,
    firm_slug: str,
    wire_bank: str,
    custom_column_map: Optional[dict] = None,
) -> Path:
    """Generate a bank-formatted wire upload CSV for a distribution."""
    records = _build_wire_records_for_distribution(distribution_id, firm_id)
    if not records:
        raise ValueError("No wire records found for this distribution.")

    return _write_wire_file(
        records=records,
        wire_bank=wire_bank,
        wire_type="Distribution",
        firm_slug=firm_slug,
        custom_column_map=custom_column_map,
    )


def generate_capital_call_wire_file(
    capital_call_id: str,
    firm_id: str,
    firm_slug: str,
    wire_bank: str,
    custom_column_map: Optional[dict] = None,
) -> Path:
    """Generate a bank-formatted wire upload CSV for a capital call."""
    records = _build_wire_records_for_capital_call(capital_call_id, firm_id)
    if not records:
        raise ValueError("No wire records found for this capital call.")

    return _write_wire_file(
        records=records,
        wire_bank=wire_bank,
        wire_type="CapitalCall",
        firm_slug=firm_slug,
        custom_column_map=custom_column_map,
    )


def _write_wire_file(
    records: list[WireRecord],
    wire_bank: str,
    wire_type: str,
    firm_slug: str,
    custom_column_map: Optional[dict] = None,
) -> Path:
    bank_key = wire_bank.lower()

    if bank_key == "custom":
        if not custom_column_map:
            raise ValueError(
                "wire_bank is 'custom' but no custom_column_map was provided. "
                "Upload a sample CSV via POST /wire/learn-template first."
            )
        adapter = CustomAdapter(custom_column_map)
    else:
        adapter_class = BANK_ADAPTERS.get(bank_key, GenericAdapter)
        adapter = adapter_class()

    output_dir = EXPORTS_DIR / firm_slug
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = output_dir / f"WireUpload_{wire_bank}_{wire_type}_{date.today().isoformat()}.csv"
    rows = [adapter.format(r) for r in records]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=adapter.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "%s wire file written: %s (%s row(s), bank=%s)",
        wire_type,
        filename,
        len(rows),
        wire_bank,
    )
    return filename
