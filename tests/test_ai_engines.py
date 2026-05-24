import importlib
import sys
import types
from unittest.mock import patch

import pytest

from core.ai_engines.base import KYCReviewer


class MockReviewer:
    name = "mock_engine"
    model_version = "mock-model-v1"

    def __init__(self):
        self.calls = []

    def review(
        self,
        file_bytes: bytes,
        *,
        requested_doc_type: str | None = None,
        entity_name: str | None = None,
    ) -> dict:
        self.calls.append(
            {
                "file_bytes": file_bytes,
                "requested_doc_type": requested_doc_type,
                "entity_name": entity_name,
            }
        )
        return {
            "document_type_detected": "Operating Agreement",
            "matches_requested_type": True,
            "confidence": "high",
            "entity_name": entity_name,
            "formation_date": None,
            "state_of_formation": None,
            "ownership_structure": {"type": "LLC"},
            "nested_entities": [],
            "signatories": [],
            "flags": [],
            "status": "Approved",
            "escalate_to_compliance": False,
        }


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self, settings: dict):
        self.settings = settings
        self.inserts = []

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.payload = None

    def select(self, _columns: str):
        return self

    def eq(self, _column: str, _value):
        return self

    def single(self):
        return self

    def insert(self, payload: dict):
        self.payload = payload
        return self

    def execute(self):
        if self.table_name == "firm_settings":
            return FakeResult(self.db.settings)
        if self.table_name == "ai_invocations":
            self.db.inserts.append(self.payload)
            return FakeResult([{**self.payload, "id": 1}])
        return FakeResult(None)


def test_mock_reviewer_implements_protocol():
    assert isinstance(MockReviewer(), KYCReviewer)


def test_registry_returns_openai_reviewer():
    from core.ai_engines.openai_vision import OpenAIVisionReviewer
    from core.ai_engines.registry import get_kyc_reviewer

    assert isinstance(get_kyc_reviewer("openai_vision"), OpenAIVisionReviewer)


def test_registry_returns_anthropic_reviewer():
    from core.ai_engines.anthropic_claude import AnthropicKYCAgent
    from core.ai_engines.registry import get_kyc_reviewer

    assert isinstance(get_kyc_reviewer("anthropic_claude"), AnthropicKYCAgent)


def test_registry_rejects_unknown_engine():
    from core.ai_engines.registry import get_kyc_reviewer

    with pytest.raises(ValueError):
        get_kyc_reviewer("unknown_engine")


def test_review_kyc_document_dispatches_and_records_invocation(firm_id_a):
    fake_supabase = FakeSupabase({"kyc_engine": "anthropic_claude"})
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    audit_ids = []
    fake_audit = types.ModuleType("core.audit")

    def fake_log_audit(**kwargs):
        audit_ids.append(kwargs)
        return 42

    fake_audit.log_audit = fake_log_audit
    mock_reviewer = MockReviewer()

    with patch.dict(
        sys.modules,
        {
            "core.database": fake_database,
            "core.audit": fake_audit,
        },
    ):
        sys.modules.pop("core.kyc_parser", None)
        kyc_parser = importlib.import_module("core.kyc_parser")

        with patch(
            "core.ai_engines.registry.get_kyc_reviewer",
            return_value=mock_reviewer,
        ) as get_reviewer:
            result = kyc_parser.review_kyc_document(
                b"pdf-bytes",
                firm_id=firm_id_a,
                requested_doc_type="Operating Agreement",
                entity_name="Test LLC",
            )

    get_reviewer.assert_called_once_with("anthropic_claude")
    assert result["status"] == "Approved"
    assert mock_reviewer.calls[0]["file_bytes"] == b"pdf-bytes"
    assert audit_ids[0]["action"] == "ai_invocation.kyc_review"
    assert len(fake_supabase.inserts) == 1
    assert fake_supabase.inserts[0]["engine"] == "mock_engine"
    assert fake_supabase.inserts[0]["model_version"] == "mock-model-v1"
    assert fake_supabase.inserts[0]["task"] == "kyc_review"
    assert fake_supabase.inserts[0]["status"] == "ok"
    assert fake_supabase.inserts[0]["audit_log_id"] == 42
