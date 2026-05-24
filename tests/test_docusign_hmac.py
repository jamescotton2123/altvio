import base64
import hashlib
import hmac
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
    def __init__(self, hmac_secret: str):
        self.hmac_secret = hmac_secret
        self.webhook_event_keys = set()

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

    def limit(self, _count: int):
        return self

    def single(self):
        return self

    def execute(self):
        if self.operation == "insert" and self.table_name == "webhook_events":
            key = (self.payload["source"], self.payload["external_id"])
            if key in self.db.webhook_event_keys:
                return FakeResult([])
            self.db.webhook_event_keys.add(key)
            return FakeResult([self.payload])

        if self.table_name == "commitments" and self._has_filter("envelope_id", "envelope-1"):
            return FakeResult([{"firm_id": "firm-1"}])

        if self.table_name == "firm_settings" and self._has_filter("firm_id", "firm-1"):
            return FakeResult(
                {
                    "firm_id": "firm-1",
                    "docusign_connect_hmac_secret": self.db.hmac_secret,
                }
            )

        return FakeResult([])

    def _has_filter(self, column: str, value) -> bool:
        return any(filter_column == column and filter_value == value for filter_column, filter_value in self.filters)


@pytest.fixture
def docusign_client():
    hmac_secret = "test-connect-secret"
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase(hmac_secret=hmac_secret)

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("api.routes.docusign_webhook", None)
        docusign_webhook = importlib.import_module("api.routes.docusign_webhook")

        app = FastAPI()
        app.include_router(docusign_webhook.router, prefix="/docusign")
        yield TestClient(app), hmac_secret


def _signature_for(payload: bytes, hmac_secret: str) -> str:
    digest = hmac.new(
        hmac_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def test_valid_signature_accepts_webhook(docusign_client):
    client, hmac_secret = docusign_client
    payload = b'{"event":"ignored-test-event","data":{"envelopeId":"envelope-1"}}'

    response = client.post(
        "/docusign/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-DocuSign-Signature-1": _signature_for(payload, hmac_secret),
        },
    )

    assert response.status_code == 200


def test_invalid_signature_rejects_webhook(docusign_client):
    client, _hmac_secret = docusign_client
    payload = b'{"event":"ignored-test-event","data":{"envelopeId":"envelope-1"}}'

    response = client.post(
        "/docusign/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-DocuSign-Signature-1": base64.b64encode(b"wrong-signature").decode("ascii"),
        },
    )

    assert response.status_code == 401
