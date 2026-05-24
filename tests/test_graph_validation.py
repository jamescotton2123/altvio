import importlib
import sys
import types
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self):
        self.investor = {
            "id": "investor-1",
            "firm_id": "firm-1",
            "entity_name": "Test Investor",
            "sharepoint_folder_id": "folder-1",
        }
        self.settings = {
            "firm_id": "firm-1",
            "ops_mailbox": "ops@example.com",
            "sharepoint_site_id": "site-1",
            "graph_subscription_client_state": "expected-client-state",
        }

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.operation = "select"
        self.payload = None

    def select(self, _columns: str):
        self.operation = "select"
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def insert(self, payload: dict):
        self.operation = "insert"
        self.payload = payload
        return self

    def upsert(self, payload: dict, **_kwargs):
        self.operation = "insert"
        self.payload = payload
        return self

    def on_conflict(self, *_args, **_kwargs):
        return self

    def single(self):
        return self

    def execute(self):
        if self.operation == "insert":
            return FakeResult([self.payload])

        if self.table_name == "investors" and self._matches(
            self.db.investor, {"sharepoint_folder_id": "folder-1"}
        ):
            return FakeResult(self.db.investor.copy())

        if self.table_name == "firm_settings":
            if self._matches(self.db.settings, {"firm_id": "firm-1"}) or self._matches(
                self.db.settings, {"ops_mailbox": "ops@example.com"}
            ):
                return FakeResult(self.db.settings.copy())

        return FakeResult(None)

    def _matches(self, row: dict, required: dict | None = None) -> bool:
        filters = self.filters
        if required:
            filters = [*filters, *required.items()]
        return all(row.get(column) == value for column, value in filters)


class FakeFileResponse:
    content = b"pdf-bytes"

    def raise_for_status(self):
        return None


@pytest.fixture
def graph_context():
    kyc_uploads = []
    email_reads = []
    onboardings = []

    def process_kyc_upload(**kwargs):
        kyc_uploads.append(kwargs)
        return {"status": "processed"}

    def get_email_body(_settings: dict, message_id: str) -> str:
        email_reads.append(message_id)
        return "Please onboard Test Investor."

    def run_onboarding(**kwargs):
        onboardings.append(kwargs)
        return {"investor_id": "investor-1"}

    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase()

    fake_graph_client = types.ModuleType("core.graph_client")
    fake_graph_client._get_access_token = lambda _settings: "graph-token"
    fake_graph_client.get_email_body = get_email_body

    fake_http_retry = types.ModuleType("core.http_retry")
    fake_http_retry.REQUEST_TIMEOUT_SECONDS = 30
    fake_http_retry.request_with_retry = lambda *_args, **_kwargs: FakeFileResponse()

    fake_kyc_parser = types.ModuleType("core.kyc_parser")
    fake_kyc_parser.process_kyc_upload = process_kyc_upload

    fake_ai_parser = types.ModuleType("core.ai_parser")
    fake_ai_parser.parse_email = lambda _raw_text: {
        "confidence": "high",
        "investor_name": "Test Investor",
    }
    fake_ai_parser.parse_form_submission = lambda payload: payload

    fake_onboarding = types.ModuleType("core.onboarding")
    fake_onboarding.run_onboarding = run_onboarding

    with patch.dict(
            sys.modules,
            {
                "core.database": fake_database,
                "core.graph_client": fake_graph_client,
                "core.http_retry": fake_http_retry,
                "core.kyc_parser": fake_kyc_parser,
                "core.ai_parser": fake_ai_parser,
                "core.onboarding": fake_onboarding,
            },
    ):
        sys.modules.pop("api.routes.kyc_webhook", None)
        sys.modules.pop("api.routes.intake", None)

        kyc_webhook = importlib.import_module("api.routes.kyc_webhook")
        intake = importlib.import_module("api.routes.intake")

        app = FastAPI()
        app.include_router(kyc_webhook.router, prefix="/kyc")
        app.include_router(intake.router, prefix="/intake")
        yield types.SimpleNamespace(
            client=TestClient(app),
            kyc_uploads=kyc_uploads,
            email_reads=email_reads,
            onboardings=onboardings,
        )


def _post_kyc_notification(client: TestClient, client_state: str):
    return client.post(
        "/kyc/webhook",
        json={
            "value": [
                {
                    "clientState": client_state,
                    "resource": "drives/drive-1/items/item-1",
                    "resourceData": {
                        "id": "item-1",
                        "name": "document.pdf",
                        "parentReference": {"id": "folder-1"},
                    },
                }
            ]
        },
    )


def _post_intake_notification(client: TestClient, client_state: str):
    return client.post(
        "/intake/email",
        json={
            "value": [
                {
                    "clientState": client_state,
                    "resource": "users/ops@example.com/mailFolders/Inbox/messages/message-1",
                }
            ]
        },
    )


def test_correct_client_state_processes_graph_notifications(graph_context):
    _post_kyc_notification(graph_context.client, "expected-client-state")
    _post_intake_notification(graph_context.client, "expected-client-state")

    assert len(graph_context.kyc_uploads) == 1
    assert graph_context.email_reads == ["message-1"]
    assert len(graph_context.onboardings) == 1


def test_wrong_client_state_skips_graph_notifications(graph_context):
    kyc_response = _post_kyc_notification(graph_context.client, "wrong-client-state")
    intake_response = _post_intake_notification(graph_context.client, "wrong-client-state")

    assert kyc_response.json()["processed"] == 0
    assert intake_response.json()["processed"] == 0
    assert graph_context.kyc_uploads == []
    assert graph_context.email_reads == []
    assert graph_context.onboardings == []
