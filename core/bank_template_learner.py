"""
AI-powered bank template learner.
Firms using smaller or regional banks upload a sample wire CSV.
GPT-4o reads the headers and sample rows, maps them to WireRecord fields,
and saves the mapping to firm_settings for all future wire generation.

The stored column map drives the CustomAdapter in wire_template_generator.py.
"""

import csv
import io
import json
import logging

from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

logger = logging.getLogger(__name__)

WIRE_RECORD_FIELDS = {
    "wire_date": "The date the wire should be executed or settled",
    "beneficiary_name": "The name of the person or entity receiving the wire",
    "beneficiary_account": "The bank account number of the recipient",
    "routing_number": "The ABA routing number or bank routing code",
    "bank_name": "The name of the receiving bank",
    "amount": "The dollar amount to wire (numeric, no currency symbols in the value)",
    "reference_memo": "The reference, memo, or OBI field for the wire",
    "beneficiary_address": "The mailing address of the beneficiary (optional)",
    "bank_address": "The address of the receiving bank (optional)",
    "intermediary_bank": "Intermediary or correspondent bank name, if applicable (optional)",
    "intermediary_routing": "Routing number for the intermediary bank, if applicable (optional)",
}

MAPPING_PROMPT = """
You are a financial operations specialist reviewing a wire transfer CSV template from a bank.

Here are the CSV headers and a few sample rows from the template:

{sample_data}

Your task is to map each CSV column to one of our internal wire record fields.
Internal fields and their meanings:
{field_descriptions}

Return ONLY a valid JSON object where:
- Keys are our internal field names (from the list above)
- Values are the exact CSV column header names from the template
- Only include fields that have a clear, confident match
- If a column could be either "amount" or something else, prefer the most specific match
- Do not include optional fields (beneficiary_address, bank_address, intermediary_bank, intermediary_routing)
  unless they clearly exist in the CSV

Example output format:
{{
  "wire_date": "Settlement Date",
  "beneficiary_name": "Payee Name",
  "beneficiary_account": "Account Number",
  "routing_number": "ABA/Routing",
  "bank_name": "Bank Name",
  "amount": "Amount",
  "reference_memo": "Reference"
}}
"""


def _parse_csv_sample(csv_bytes: bytes, sample_rows: int = 3) -> tuple[list[str], list[dict]]:
    """Extract headers and first N data rows from a CSV file."""
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = []
    for i, row in enumerate(reader):
        if i >= sample_rows:
            break
        rows.append(dict(row))
    return list(headers), rows


def learn_bank_template(csv_bytes: bytes, sample_rows: int = 3) -> dict:
    """
    Analyze a sample bank wire CSV and return a column mapping.

    Args:
        csv_bytes: Raw bytes of the uploaded CSV file.
        sample_rows: Number of data rows to include as examples for GPT-4o.

    Returns:
        A dict mapping internal WireRecord field names to CSV column headers.
        Example: { "wire_date": "Settlement Date", "amount": "Wire Amount USD", ... }
    """
    headers, rows = _parse_csv_sample(csv_bytes, sample_rows)

    if not headers:
        raise ValueError("Could not parse CSV headers from the uploaded file.")

    # Build readable sample for the prompt
    sample_lines = ["Headers: " + ", ".join(headers)]
    for i, row in enumerate(rows, 1):
        sample_lines.append(f"Row {i}: " + " | ".join(f"{k}: {v}" for k, v in row.items() if v))
    sample_data = "\n".join(sample_lines)

    field_descriptions = "\n".join(
        f"  - {field}: {desc}" for field, desc in WIRE_RECORD_FIELDS.items()
    )

    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": MAPPING_PROMPT.format(
                    sample_data=sample_data,
                    field_descriptions=field_descriptions,
                ),
            }
        ],
        temperature=0,
    )

    mapping = json.loads(response.choices[0].message.content)

    # Validate that required fields are mapped
    required = {"wire_date", "beneficiary_name", "beneficiary_account", "routing_number", "bank_name", "amount", "reference_memo"}
    missing = required - set(mapping.keys())
    if missing:
        raise ValueError(
            f"Could not confidently map required fields from the CSV: {missing}. "
            "Please ensure the CSV contains columns for these fields."
        )

    # Validate all mapped values exist in the actual CSV headers
    invalid = {k: v for k, v in mapping.items() if v not in headers}
    if invalid:
        raise ValueError(
            f"Mapped column names not found in CSV headers: {invalid}. "
            f"Available headers: {headers}"
        )

    return mapping


def save_bank_template(
    firm_id: str,
    bank_name: str,
    column_map: dict,
) -> None:
    """Persist the learned column map to firm_settings and activate the custom adapter."""
    from core.database import supabase

    supabase.table("firm_settings").update({
        "custom_bank_name": bank_name,
        "custom_wire_column_map": column_map,
        "wire_bank": "custom",
    }).eq("firm_id", firm_id).execute()

    logger.info("Custom bank template saved for firm %s. bank=%s", firm_id, bank_name)
