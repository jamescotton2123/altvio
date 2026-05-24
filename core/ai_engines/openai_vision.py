import base64
import json
import os

from openai import OpenAI

from core.http_retry import AI_CLIENT_TIMEOUT_SECONDS, openai_chat_completion_with_retry
from core.kyc_parser import KYC_REVIEW_PROMPT


class OpenAIVisionReviewer:
    name = "openai_vision"
    model_version = "gpt-4o"

    def __init__(self) -> None:
        self.client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY") or "missing-openai-api-key",
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

        response = openai_chat_completion_with_retry(
            self.client.chat.completions.create,
            model=self.model_version,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
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

        return json.loads(response.choices[0].message.content)


def _review_context(requested_doc_type: str | None, entity_name: str | None) -> str:
    context = ""
    if requested_doc_type:
        context += f"\nWe requested a: {requested_doc_type}."
    if entity_name:
        context += f"\nThe investor entity name on file is: {entity_name}."
    if context:
        return "\n\nContext:" + context
    return ""
