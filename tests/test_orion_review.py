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


class FakeQuery:
    def __init__(self, db: "FakeSupabase", table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters: list[tuple[str, object]] = []
        self.in_filters: list[tuple[str, list]] = []
        self.operation = "select"
        self.payload = None
        self.return_single = False
        self.order_column = None
        self.order_desc = False

    def select(self, _columns: str):
        self.operation = "select"
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def in_(self, column: str, values: list):
        self.in_filters.append((column, values))
        return self

    def order(self, column: str, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
        return self

    def single(self):
        self.return_single = True
        return self

    def update(self, payload: dict):
        self.operation = "update"
        self.payload = payload
        return self

    def execute(self):
        if self.operation == "update":
            return self._update()
        rows = self._select()
        if self.order_column:
            rows = sorted(
                rows,
                key=lambda row: row.get(self.order_column) or "",
                reverse=self.order_desc,
            )
        if self.return_single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)

    def _select(self):
        rows = self.db.rows_by_table.get(self.table_name, [])
        return [row for row in rows if self._matches(row)]

    def _matches(self, row: dict) -> bool:
        for column, value in self.filters:
            if row.get(column) != value:
                return False
        for column, values in self.in_filters:
            if row.get(column) not in values:
                return False
        return True

    def _update(self):
        rows = self._select()
        for row in rows:
            row.update(self.payload)
        return FakeResult(rows)


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, list[dict]]):
        self.rows_by_table = rows_by_table

    def table(self, name: str):
        return FakeQuery(self, name)


FIRM_ID = "firm-1"
INVESTOR_ID = "inv-1"


@pytest.fixture
def orion_review_client():
    rows_by_table = {
        "investors": [
            {
                "id": INVESTOR_ID,
                "firm_id": FIRM_ID,
                "entity_name": "Smith Family Trust",
                "orion_match_status": "Needs Review",
                "orion_household_name": None,
                "orion_review_notes": None,
                "created_at": "2026-05-01T12:00:00Z",
            },
            {
                "id": "inv-2",
                "firm_id": FIRM_ID,
                "entity_name": "Other Fund LLC",
                "orion_match_status": "Confirmed",
                "created_at": "2026-05-02T12:00:00Z",
            },
        ],
        "orion_match_candidates": [
            {
                "firm_id": FIRM_ID,
                "investor_id": INVESTOR_ID,
                "candidates": [
                    {"name": "Smith Household", "score": 88.5},
                    {"name": "Smith Family", "score": 82.0},
                    {"name": "S. Smith", "score": 79.1},
                    {"name": "Extra Match", "score": 70.0},
                ],
                "created_at": "2026-05-01T12:05:00Z",
            }
        ],
        "audit_logs": [],
    }
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase(rows_by_table)

    fake_orion_matcher = types.ModuleType("core.orion_matcher")

    def fake_confirm_match(investor_id: str, confirmed_name: str, reviewed_by=None):
        for row in rows_by_table["investors"]:
            if row["id"] == investor_id:
                row["orion_household_name"] = confirmed_name
                row["orion_match_status"] = "Confirmed"
        return {"status": "confirmed", "investor_id": investor_id, "household_name": confirmed_name}

    fake_orion_matcher.confirm_match = fake_confirm_match

    fake_audit = types.ModuleType("core.audit")
    fake_audit.log_audit = lambda **kwargs: "audit-1"

    with patch.dict(
        sys.modules,
        {
            "core.database": fake_database,
            "core.orion_matcher": fake_orion_matcher,
            "core.audit": fake_audit,
        },
    ):
        sys.modules.pop("api.routes.imports_exports", None)
        imports_exports = importlib.import_module("api.routes.imports_exports")

        app = FastAPI()
        app.include_router(imports_exports.router)
        yield TestClient(app)


def test_orion_review_queue_requires_firm_header(orion_review_client):
    response = orion_review_client.get("/orion/review-queue")
    assert response.status_code == 401


def test_orion_review_queue_returns_pending_investors(orion_review_client):
    response = orion_review_client.get(
        "/orion/review-queue",
        headers={"X-Firm-ID": FIRM_ID},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0] == {
        "investor_id": INVESTOR_ID,
        "entity_name": "Smith Family Trust",
        "current_orion_match_status": "Needs Review",
        "candidates": [
            {"name": "Smith Household", "score": 88.5},
            {"name": "Smith Family", "score": 82.0},
            {"name": "S. Smith", "score": 79.1},
        ],
        "created_at": "2026-05-01T12:05:00Z",
    }


def test_orion_review_confirm_updates_status(orion_review_client):
    response = orion_review_client.post(
        f"/orion/review-queue/{INVESTOR_ID}/confirm",
        headers={"X-Firm-ID": FIRM_ID},
        json={"household_name": "Smith Household"},
    )

    assert response.status_code == 200
    assert response.json()["orion_match_status"] == "Confirmed"
    assert response.json()["orion_household_name"] == "Smith Household"
