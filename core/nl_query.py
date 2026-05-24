"""
Natural-language → allowlisted read-only RPCs for ops/advisor questions.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.database import supabase
from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

_INTENT_PROMPT = """
Classify the user's ops/advisor question into exactly one safe query function and extract parameters.

Safe functions:
- query_investor_kyc_status_counts: count investors by KYC status; optional p_kyc_status.
- query_commitments_by_status: list commitments by ledger status; required p_status.
- query_commitment_funding_status: list commitments by wire/funding status; required p_wire_status.
- query_commitments_by_docusign_status: list commitments by DocuSign status; required p_docusign_status.
- query_aum_by_advisor: committed/funded/fee totals grouped by advisor; optional p_advisor_email.
- query_deal_raise_progress: target raise and committed/funded progress by deal; optional p_deal_id or p_offering_name.
- query_distribution_notices: distribution notice amounts/status; optional p_status or p_deal_id.
- query_handle_with_care_investors: list investors flagged handle_with_care; no parameters.
- unknown_intent: unsupported, raw SQL, write attempts, schema probing, or anything outside the safe functions.

Rules:
- Return only a safe function name and parameters.
- Never return SQL.
- Never include firm_id. It is supplied by the authenticated request context.
- Default p_limit to 100 for listing functions unless the user asks for a smaller limit.
"""

_UNKNOWN_INTENT = "unknown_intent"

_INTENT_PARAMETERS: dict[str, set[str]] = {
    "query_investor_kyc_status_counts": {"p_kyc_status"},
    "query_commitments_by_status": {"p_status", "p_limit"},
    "query_commitment_funding_status": {"p_wire_status", "p_limit"},
    "query_commitments_by_docusign_status": {"p_docusign_status", "p_limit"},
    "query_aum_by_advisor": {"p_advisor_email"},
    "query_deal_raise_progress": {"p_deal_id", "p_offering_name"},
    "query_distribution_notices": {"p_status", "p_deal_id", "p_limit"},
    "query_handle_with_care_investors": {"p_limit"},
}

_RAW_SQL_PATTERN = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|"
    r"REPLACE|EXECUTE|CALL|COPY|MERGE)\b|;|--|/\*|\*/",
    re.IGNORECASE,
)

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_uuid(value: str, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not _UUID_PATTERN.match(cleaned):
        raise ValueError(f"Invalid {field_name}.")
    return cleaned


def _validate_firm_id(firm_id: str) -> str:
    return _validate_uuid(firm_id, "firm_id")


def _classify_question(question: str) -> dict[str, Any]:
    if _RAW_SQL_PATTERN.search(question):
        return {"function_name": _UNKNOWN_INTENT, "parameters": {}}

    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _INTENT_PROMPT},
            {
                "role": "user",
                "content": f"Classify this question: {question}",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "submit_query_intent",
                    "description": "Submit the safe query function and parameters.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "function_name": {
                                "type": "string",
                                "enum": [*_INTENT_PARAMETERS.keys(), _UNKNOWN_INTENT],
                            },
                            "parameters": {"type": "object"},
                        },
                        "required": ["function_name", "parameters"],
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "submit_query_intent"}},
        temperature=0,
    )
    msg = response.choices[0].message
    if not msg.tool_calls:
        raise ValueError("Model did not return a query intent.")
    return json.loads(msg.tool_calls[0].function.arguments)


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 100
    return max(1, min(limit, 500))


def _normalize_parameters(function_name: str, raw_parameters: Any) -> dict[str, Any]:
    if function_name not in _INTENT_PARAMETERS:
        raise ValueError("Unsupported query intent.")
    if not isinstance(raw_parameters, dict):
        raw_parameters = {}

    allowed = _INTENT_PARAMETERS[function_name]
    parameters: dict[str, Any] = {}
    for key in allowed:
        value = raw_parameters.get(key)
        if key == "p_limit":
            parameters[key] = _normalize_limit(value)
            continue
        if key == "p_deal_id":
            deal_id = _normalize_text(value)
            if deal_id is not None:
                parameters[key] = _validate_uuid(deal_id, "p_deal_id")
            continue
        normalized = _normalize_text(value)
        if normalized is not None:
            parameters[key] = normalized

    required_by_intent = {
        "query_commitments_by_status": "p_status",
        "query_commitment_funding_status": "p_wire_status",
        "query_commitments_by_docusign_status": "p_docusign_status",
    }
    required = required_by_intent.get(function_name)
    if required and required not in parameters:
        raise ValueError(f"Missing required parameter for {function_name}: {required}")

    return parameters


def _execute_query(function_name: str, firm_id: str, parameters: dict[str, Any]) -> list[dict]:
    rpc_parameters = {"p_firm_id": firm_id, **parameters}
    result = supabase.rpc(function_name, rpc_parameters).execute()
    rows = result.data or []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append({"value": row})
    return out


def _summarize_results(question: str, results: list[dict]) -> str:
    preview = results[:5]
    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": (
                    f"The user asked: {question}\n\n"
                    f"Summarize these results in one concise sentence for an ops user "
                    f"({len(results)} total row(s)):\n{json.dumps(preview, default=str)}"
                ),
            }
        ],
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()


def run_nl_query(question: str, firm_id: str) -> dict[str, Any]:
    """
    Returns:
    {
        "function": str,
        "parameters": dict,
        "results": list[dict],
        "summary": str,
        "row_count": int
    }
    """
    fid = _validate_firm_id(firm_id)
    q = (question or "").strip()
    if not q:
        raise ValueError("question is required.")

    intent = _classify_question(q)
    function_name = intent.get("function_name")
    if function_name == _UNKNOWN_INTENT:
        raise ValueError("Unsupported or unsafe natural-language query.")
    parameters = _normalize_parameters(function_name, intent.get("parameters"))
    results = _execute_query(function_name, fid, parameters)
    summary = _summarize_results(q, results) if results else "No rows matched your question."

    return {
        "function": function_name,
        "parameters": parameters,
        "results": results,
        "summary": summary,
        "row_count": len(results),
    }
