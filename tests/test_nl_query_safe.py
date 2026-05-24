import importlib
import json
import sys
import types
from unittest.mock import patch

import pytest

FIRM_ID = "11111111-1111-1111-1111-111111111111"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeRpc:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return FakeResult(self.data)


class FakeSupabase:
    def __init__(self):
        self.calls = []

    def rpc(self, function_name: str, parameters: dict):
        self.calls.append((function_name, parameters))
        return FakeRpc([{"kyc_status": "Pending", "investor_count": 2}])


class FakeOpenAI:
    def __init__(self, **_kwargs):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **_kwargs: None)
        )


class FakeFunction:
    def __init__(self, arguments: dict):
        self.arguments = json.dumps(arguments)


class FakeToolCall:
    def __init__(self, arguments: dict):
        self.function = FakeFunction(arguments)


class FakeMessage:
    def __init__(self, arguments: dict | None = None, content: str = ""):
        self.tool_calls = [FakeToolCall(arguments)] if arguments else []
        self.content = content


class FakeChoice:
    def __init__(self, message: FakeMessage):
        self.message = message


class FakeResponse:
    def __init__(self, message: FakeMessage):
        self.choices = [FakeChoice(message)]


def _fake_openai_retry(_create, **kwargs):
    if kwargs.get("tools"):
        return FakeResponse(
            FakeMessage(
                {
                    "function_name": "query_investor_kyc_status_counts",
                    "parameters": {"p_kyc_status": "Pending"},
                }
            )
        )
    return FakeResponse(FakeMessage(content="There are 2 investors pending KYC."))


@pytest.fixture
def nl_query_context():
    fake_supabase = FakeSupabase()
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI

    fake_http_retry = types.ModuleType("core.http_retry")
    fake_http_retry.AI_CLIENT_TIMEOUT_SECONDS = 30
    fake_http_retry.openai_chat_completion_with_retry = _fake_openai_retry

    fake_openai_client = types.ModuleType("core.openai_client")
    fake_openai_client.get_openai_client = lambda: FakeOpenAI()

    with patch.dict(
            sys.modules,
            {
                "core.database": fake_database,
                "core.http_retry": fake_http_retry,
                "core.openai_client": fake_openai_client,
                "openai": fake_openai,
            },
    ):
        sys.modules.pop("core.nl_query", None)
        nl_query = importlib.import_module("core.nl_query")
        yield types.SimpleNamespace(nl_query=nl_query, fake_supabase=fake_supabase)
        sys.modules.pop("core.nl_query", None)


def test_pending_kyc_question_routes_to_safe_rpc(nl_query_context):
    result = nl_query_context.nl_query.run_nl_query("how many investors are pending KYC", FIRM_ID)

    assert result["function"] == "query_investor_kyc_status_counts"
    assert result["parameters"] == {"p_kyc_status": "Pending"}
    assert result["row_count"] == 1
    assert nl_query_context.fake_supabase.calls == [
        (
            "query_investor_kyc_status_counts",
            {"p_firm_id": FIRM_ID, "p_kyc_status": "Pending"},
        )
    ]


def test_raw_sql_question_is_rejected_before_rpc(nl_query_context):
    with pytest.raises(ValueError, match="Unsupported or unsafe"):
        nl_query_context.nl_query.run_nl_query(
            "DROP TABLE investors; SELECT * FROM investors",
            FIRM_ID,
        )

    assert nl_query_context.fake_supabase.calls == []
