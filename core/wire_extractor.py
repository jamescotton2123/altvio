"""
Extract distribution payout banking from a signed subscription PDF (GPT-4o vision).

Stores to investors.wire_instructions — where the FIRM sends future distributions TO the investor.
This is NOT the inbound subscription wire (fund receiving account on the deal) and NOT
commitments.funding_entity_name (legal entity the capital was sent FROM). Record inbound
funding source separately via PATCH /commitments/{id} or /fund?funding_entity_name=...

Used after envelope completion (optional firm toggle) and via POST /commitments/{id}/extract-wire.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from core.database import supabase
from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

_WIRE_PROMPT = """
You are parsing wire transfer instructions from a signed alternative investment subscription agreement PDF.

Return a single JSON object with exactly these keys (use null if not found):
{
  "beneficiary_name": string | null,
  "account_number": string | null,
  "routing_number": string | null,
  "bank_name": string | null,
  "bank_address": string | null
}

Extract values exactly as printed on the wire instructions section (beneficiary may appear as account title).
routing_number is the domestic ABA/routing number if shown (digits only or formatted).
Do not invent values.
"""


def _normalize_digits(val: Any) -> str | None:
    if val is None:
        return None
    s = "".join(c for c in str(val) if c.isdigit())
    return s or None


def _identifiers_from_wire(wire: dict | None) -> tuple[str | None, str | None]:
    if not wire or not isinstance(wire, dict):
        return None, None
    return _normalize_digits(wire.get("account_number")), _normalize_digits(wire.get("routing_number"))


def _wire_effectively_empty(current: Any) -> bool:
    """True when ops has no structured routing/account on file (treat like missing)."""
    if current is None:
        return True
    if isinstance(current, str):
        return not current.strip()
    if isinstance(current, dict):
        acct, rt = _identifiers_from_wire(current)
        return not acct and not rt
    return True


def _identifiers_conflict(ext_wire: dict, cur_wire: dict) -> bool:
    """Conflict when both disagree on an identifier, or PDF introduces routing/account where DB lacked one."""
    ea, er = _identifiers_from_wire(ext_wire)
    ca, cr = _identifiers_from_wire(cur_wire)
    if ea and ca and ea != ca:
        return True
    if er and cr and er != cr:
        return True
    if ea and not ca:
        return True
    if er and not cr:
        return True
    return False


def _extract_wire_ai(pdf_bytes: bytes) -> dict[str, str | None]:
    encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _WIRE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:application/pdf;base64,{encoded}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        temperature=0,
    )
    raw = json.loads(response.choices[0].message.content)
    keys = ("beneficiary_name", "account_number", "routing_number", "bank_name", "bank_address")

    def _norm_field(v: Any) -> str | None:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v)
        if isinstance(v, str):
            s = v.strip()
            return s or None
        return None

    return {k: _norm_field(raw.get(k)) for k in keys}


def _to_investor_wire_shape(ext: dict[str, str | None]) -> dict[str, Any]:
    """Align with portal / deal_hub JSONB wire_instructions (account_name vs beneficiary_name)."""
    out: dict[str, Any] = {}
    if ext.get("bank_name"):
        out["bank_name"] = ext["bank_name"].strip()
    if ext.get("bank_address"):
        out["bank_address"] = ext["bank_address"].strip()
    if ext.get("routing_number"):
        out["routing_number"] = str(ext["routing_number"]).strip()
    if ext.get("account_number"):
        out["account_number"] = str(ext["account_number"]).strip()
    bn = ext.get("beneficiary_name")
    if bn:
        out["account_name"] = bn.strip()
    return out


def _write_commitment_extracted(commitment_id: str, payload: dict) -> None:
    supabase.table("commitments").update({
        "wire_instructions_extracted": payload,
    }).eq("id", commitment_id).execute()


def extract_wire_from_pdf(
    investor_id: str,
    commitment_id: str,
    firm_id: str,
    pdf_bytes: bytes,
    settings: dict,
) -> dict:
    """
    GPT-4o Vision extracts wire instructions from signed sub doc PDF.
    Returns:
    {
        "extracted": {
            "beneficiary_name": str | None,
            "account_number": str | None,
            "routing_number": str | None,
            "bank_name": str | None,
            "bank_address": str | None
        },
        "discrepancy_detected": bool,
        "action": "saved_new" | "pending_review" | "no_change"
    }
    """
    extracted_flat = _extract_wire_ai(pdf_bytes)
    result_shell = {
        "extracted": extracted_flat,
        "discrepancy_detected": False,
        "action": "no_change",
    }

    wire_shape = _to_investor_wire_shape(extracted_flat)
    ext_acct, ext_rt = _identifiers_from_wire(wire_shape)
    has_meaningful = bool(ext_acct or ext_rt or wire_shape.get("bank_name"))

    inv_row = (
        supabase.table("investors")
        .select("id, wire_instructions")
        .eq("id", investor_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not inv_row:
        result_shell["action"] = "no_change"
        _write_commitment_extracted(commitment_id, {**result_shell, "error": "investor_not_found"})
        return result_shell

    current = inv_row.get("wire_instructions")

    def finalize(action: str, discrepancy: bool) -> dict:
        result_shell["action"] = action
        result_shell["discrepancy_detected"] = discrepancy
        _write_commitment_extracted(commitment_id, {**result_shell})
        return result_shell

    if not has_meaningful:
        return finalize("no_change", False)

    # Nothing structured on file — persist extraction (fund-level wire often emailed separately).
    if _wire_effectively_empty(current):
        supabase.table("investors").update({"wire_instructions": wire_shape}).eq("id", investor_id).execute()
        try:
            from core.pw_liquidation import refresh_pw_liquidation_for_investor

            refresh_pw_liquidation_for_investor(investor_id, firm_id)
        except Exception:
            pass
        return finalize("saved_new", False)

    if isinstance(current, str):
        existing_pending = (
            supabase.table("investor_pending_changes")
            .select("id")
            .eq("investor_id", investor_id)
            .eq("firm_id", firm_id)
            .eq("field_name", "wire_instructions")
            .eq("status", "Pending")
            .execute()
            .data
        )
        if existing_pending:
            return finalize("pending_review", True)
        supabase.table("investor_pending_changes").insert({
            "firm_id": firm_id,
            "investor_id": investor_id,
            "field_name": "wire_instructions",
            "current_value": current,
            "proposed_value": json.dumps(wire_shape),
            "source": "docusign_subdoc",
            "status": "Pending",
        }).execute()
        return finalize("pending_review", True)

    if not isinstance(current, dict):
        return finalize("pending_review", True)

    if not _identifiers_conflict(wire_shape, current):
        return finalize("no_change", False)

    existing_pending = (
        supabase.table("investor_pending_changes")
        .select("id")
        .eq("investor_id", investor_id)
        .eq("firm_id", firm_id)
        .eq("field_name", "wire_instructions")
        .eq("status", "Pending")
        .execute()
        .data
    )
    if existing_pending:
        return finalize("pending_review", True)

    supabase.table("investor_pending_changes").insert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "field_name": "wire_instructions",
        "current_value": json.dumps(current) if isinstance(current, dict) else current,
        "proposed_value": json.dumps(wire_shape),
        "source": "docusign_subdoc",
        "status": "Pending",
    }).execute()
    return finalize("pending_review", True)
