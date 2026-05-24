"""
Lazy OpenAI client factory.

The client is NOT created at import time — it's created on first use.
This lets the server start without OPENAI_API_KEY set, which is useful
during local development when AI features aren't being tested.

If OPENAI_API_KEY is missing when an AI call is actually made, the caller
gets a clear RuntimeError instead of a silent module-load crash.
"""

import os
from typing import Optional

from openai import OpenAI

from core.http_retry import AI_CLIENT_TIMEOUT_SECONDS

_openai_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """Return the shared OpenAI client, creating it on first call."""
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to your .env file to use AI features."
            )
        _openai_client = OpenAI(api_key=api_key, timeout=AI_CLIENT_TIMEOUT_SECONDS)
    return _openai_client
