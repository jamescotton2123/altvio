import json
import logging

from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an expert financial operations assistant for an alternative investment firm.
Your job is to extract structured investor onboarding data from unstructured advisor emails or messages.

Extract the following fields when present:
- investor_name: Full legal name of the investor or entity
- fund_name: Name of the fund or deal they want to invest in
- committed_amount: Dollar amount of the commitment (as a number, no commas or $)
- advisor_email: Email address of the advisor sending the request
- investor_email: Email address of the investor, if mentioned
- entity_type: One of Individual, LLC, Trust, LP, Corporation, or Other
- notes: Any other relevant context (urgency, special instructions, etc.)

Return ONLY a valid JSON object. If a field cannot be determined, set it to null.
Include a "confidence" field: "high" if all key fields are clear, "medium" if some guessing was required, "low" if the message is very ambiguous.
"""


def parse_email(raw_text: str) -> dict:
    """
    Parse a raw advisor email or message and extract structured onboarding data.
    Returns a dict with investor_name, fund_name, committed_amount, etc.
    Low-confidence results should be routed to ops review before auto-firing.
    """
    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        temperature=0,
    )

    result = json.loads(response.choices[0].message.content)
    return result


def parse_form_submission(form_data: dict) -> dict:
    """
    Map a structured Microsoft Form submission directly to the onboarding payload.
    No AI needed — fields are already clean. Normalizes to the same shape as parse_email().
    """
    return {
        "investor_name": form_data.get("investor_name") or form_data.get("Entity Legal Name"),
        "fund_name": form_data.get("fund_name") or form_data.get("Fund Name"),
        "committed_amount": float(form_data.get("committed_amount") or form_data.get("Commitment Amount") or 0),
        "advisor_email": form_data.get("advisor_email") or form_data.get("Advisor Email"),
        "investor_email": form_data.get("investor_email") or form_data.get("Investor Email"),
        "entity_type": form_data.get("entity_type") or form_data.get("Entity Type"),
        "notes": form_data.get("notes"),
        "confidence": "high",
    }


if __name__ == "__main__":
    sample = (
        "Hey Ops, Can we onboard John Jason into Alpha Fund for $50,000? "
        "Please request KYC and get the process started thank you!"
    )
    result = parse_email(sample)
    logger.info("Parsed sample email: %s", json.dumps(result, indent=2))
