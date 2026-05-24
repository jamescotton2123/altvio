import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self):
        self.followup_tokens = []
        self.settings = {
            "firm_id": "00000000-0000-0000-0000-000000000001",
            "ops_mailbox": "ops@example.com",
        }
        self._next_token_id = 1

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.want_single = False
        self.row_limit = None

    def select(self, _columns: str):
        self.operation = "select"
        return self

    def insert(self, payload: dict):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload: dict):
        self.operation = "update"
        self.payload = payload
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def single(self):
        self.want_single = True
        return self

    def limit(self, count: int):
        self.row_limit = count
        return self

    def execute(self):
        if self.operation == "insert":
            return self._insert()
        if self.operation == "update":
            return self._update()

        rows = self._select()
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        if self.want_single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)

    def _select(self):
        if self.table_name == "followup_tokens":
            return [row.copy() for row in self.db.followup_tokens if self._matches(row)]

        if self.table_name == "firm_settings" and self._matches(self.db.settings):
            return [self.db.settings.copy()]

        return []

    def _insert(self):
        if self.table_name == "followup_tokens":
            row = {
                "id": f"token-{self.db._next_token_id}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "used_at": None,
                **self.payload,
            }
            self.db._next_token_id += 1
            self.db.followup_tokens.append(row)
            return FakeResult([row.copy()])

        return FakeResult([])

    def _update(self):
        rows = []
        if self.table_name == "followup_tokens":
            for row in self.db.followup_tokens:
                if self._matches(row):
                    row.update(self.payload)
                    rows.append(row.copy())
        return FakeResult(rows)

    def _matches(self, row: dict) -> bool:
        return all(row.get(column) == value for column, value in self.filters)


@pytest.fixture
def followup_context():
    fake_supabase = FakeSupabase()
    sent_followups = []

    def send_kyc_followup(**kwargs):
        sent_followups.append(("kyc", kwargs))

    def send_wire_followup(**kwargs):
        sent_followups.append(("wire", kwargs))

    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    fake_scheduler = types.ModuleType("core.followup_scheduler")
    fake_scheduler.send_kyc_followup = send_kyc_followup
    fake_scheduler.send_wire_followup = send_wire_followup

    with patch.dict(
            sys.modules,
            {
                "core.database": fake_database,
                "core.followup_scheduler": fake_scheduler,
            },
    ):
        sys.modules.pop("core.followup_tokens", None)
        sys.modules.pop("api.routes.followup", None)

        tokens = importlib.import_module("core.followup_tokens")
        followup_route = importlib.import_module("api.routes.followup")

        app = FastAPI()
        app.include_router(followup_route.router, prefix="/followup")
        yield types.SimpleNamespace(
            client=TestClient(app),
            fake_supabase=fake_supabase,
            sent_followups=sent_followups,
            tokens=tokens,
        )


def _mint_kyc_token(context) -> str:
    return context.tokens.mint_followup_token(
        firm_id=context.fake_supabase.settings["firm_id"],
        type="kyc",
        investor_id="investor-1",
    )


def test_valid_token_triggers_action_and_marks_used(followup_context):
    raw_token = _mint_kyc_token(followup_context)

    response = followup_context.client.post(f"/followup/{raw_token}/approve")

    assert response.status_code == 200
    assert len(followup_context.sent_followups) == 1
    action, kwargs = followup_context.sent_followups[0]
    assert action == "kyc"
    assert kwargs["investor_id"] == "investor-1"
    assert kwargs["firm_id"] == followup_context.fake_supabase.settings["firm_id"]
    assert kwargs["settings"] == followup_context.fake_supabase.settings
    assert followup_context.fake_supabase.followup_tokens[0]["used_at"] is not None


def test_expired_token_returns_400(followup_context):
    raw_token = _mint_kyc_token(followup_context)
    followup_context.fake_supabase.followup_tokens[0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    response = followup_context.client.post(f"/followup/{raw_token}/approve")

    assert response.status_code == 400
    assert followup_context.sent_followups == []
    assert followup_context.fake_supabase.followup_tokens[0]["used_at"] is None


def test_already_used_token_returns_400(followup_context):
    raw_token = _mint_kyc_token(followup_context)
    followup_context.fake_supabase.followup_tokens[0]["used_at"] = datetime.now(timezone.utc).isoformat()

    response = followup_context.client.post(f"/followup/{raw_token}/approve")

    assert response.status_code == 400
    assert followup_context.sent_followups == []


def test_fake_token_returns_400(followup_context):
    response = followup_context.client.post("/followup/not-a-real-token/approve")

    assert response.status_code == 400
    assert followup_context.sent_followups == []
