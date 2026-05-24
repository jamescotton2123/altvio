import importlib
import sys
import types
from unittest.mock import patch

import bcrypt
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, list[dict]]):
        self.rows_by_table = rows_by_table
        self.query_log = []

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.operation = "select"
        self.payload = None
        self.return_single = False

    def select(self, _columns: str):
        self.operation = "select"
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def single(self):
        self.return_single = True
        return self

    def update(self, payload: dict):
        self.operation = "update"
        self.payload = payload
        return self

    def execute(self):
        self.db.query_log.append((self.table_name, list(self.filters), self.operation))
        if self.operation == "update":
            return self._update()

        rows = self._select()
        if self.return_single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)

    def _select(self):
        return [
            row
            for row in self.db.rows_by_table.get(self.table_name, [])
            if all(row.get(column) == value for column, value in self.filters)
        ]

    def _update(self):
        rows = self._select()
        for row in rows:
            row.update(self.payload)
        return FakeResult(rows)


def test_bcrypt_hash_of_known_key_verifies_correctly():
    raw_key = "trd_known_test_key_12345678"
    hashed = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()

    assert bcrypt.checkpw(raw_key.encode(), hashed.encode())


def test_wrong_key_fails_bcrypt_check():
    raw_key = "trd_known_test_key_12345678"
    hashed = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()

    assert not bcrypt.checkpw("trd_wrong_key_12345678".encode(), hashed.encode())


def test_lookup_by_last8_returns_correct_row():
    raw_key = "trd_lookup_key_abcdefgh"
    fake_supabase = FakeSupabase(
        {
            "traders": [
                {
                    "id": "trader-1",
                    "firm_id": "firm-1",
                    "is_active": True,
                    "api_key_last8": raw_key[-8:],
                    "api_key_hash": bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode(),
                }
            ]
        }
    )
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("core.trader_liquidation_digest", None)
        sys.modules.pop("api.routes.trader_portal", None)
        trader_portal = importlib.import_module("api.routes.trader_portal")

    trader, firm_id = trader_portal._resolve_trader(raw_key)

    assert trader["id"] == "trader-1"
    assert firm_id == "firm-1"
    assert (
        "traders",
        [("api_key_last8", raw_key[-8:]), ("is_active", True)],
        "select",
    ) in fake_supabase.query_log


def test_rotation_endpoint_rejects_request_without_existing_key():
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase({"traders": []})

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("core.trader_liquidation_digest", None)
        sys.modules.pop("api.routes.trader_portal", None)
        trader_portal = importlib.import_module("api.routes.trader_portal")

    app = FastAPI()
    app.include_router(trader_portal.router, prefix="/trader")
    client = TestClient(app)

    response = client.post(
        "/trader/generate-api-key?trader_id=trader-1",
        headers={"X-Firm-ID": "firm-1"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Bearer token required to rotate API key."
