from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

REQUEST_TIMEOUT_SECONDS = 30
AI_CLIENT_TIMEOUT_SECONDS = 60.0

_external_http_retry = retry(
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    reraise=True,
)


@_external_http_retry
def request_with_retry(method: Callable[..., requests.Response], *args: Any, **kwargs: Any) -> requests.Response:
    return method(*args, **kwargs)


@_external_http_retry
def openai_chat_completion_with_retry(create_method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return create_method(*args, **kwargs)
