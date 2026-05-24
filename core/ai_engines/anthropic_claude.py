import base64
import json
import os

from anthropic import Anthropic

from core.http_retry import AI_CLIENT_TIMEOUT_SECONDS
from core.kyc_parser import KYC_REVIEW_PROMPT


class AnthropicKYCAgent:
    name = "anthropic_claude"
    model_version = "claude-opus-4-5"

    def __init__(self) -> None:
        self.client = Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY") or "missing-anthropic-api-key",
            timeout=AI_CLIENT_TIMEOUT_SECONDS,
        )

    def review(
        self,
        file_bytes: bytes,
        *,
        requested_doc_type: str | None = None,
        entity_name: str | None = None,
    ) -> dict:
        encoded = base64.standard_b64encode(file_bytes).decode("utf-8")
        prompt = KYC_REVIEW_PROMPT + _review_context(requested_doc_type, entity_name)

        response = self.client.messages.create(
            model=self.model_version,
            max_tokens=4096,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": encoded,
                            },
                        },
                    ],
                }
            ],
        )

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return json.loads(text)


def _review_context(requested_doc_type: str | None, entity_name: str | None) -> str:
    context = ""
    if requested_doc_type:
        context += f"\nWe requested a: {requested_doc_type}."
    if entity_name:
        context += f"\nThe investor entity name on file is: {entity_name}."
    if context:
        return "\n\nContext:" + context
    return ""
