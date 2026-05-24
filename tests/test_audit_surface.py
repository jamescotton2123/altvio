import importlib
import sys
import types
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, list[dict]]):
        self.rows_by_table = rows_by_table

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.gte_filters = []
        self.order_column = None
        self.order_desc = False
        self.range_start = None
        self.range_end = None
        self.exact_count = False

    def select(self, _columns: str, count: str | None = None):
        self.exact_count = count == "exact"
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def gte(self, column: str, value):
        self.gte_filters.append((column, value))
        return self

    def order(self, column: str, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
        return self

    def range(self, start: int, end: int):
        self.range_start = start
        self.range_end = end
        return self

    def execute(self):
        rows = [
            row.copy()
            for row in self.db.rows_by_table.get(self.table_name, [])
            if self._matches(row)
        ]
        total = len(rows)

        if self.order_column:
            rows = sorted(
                rows,
                key=lambda row: row[self.order_column],
                reverse=self.order_desc,
            )
        if self.range_start is not None and self.range_end is not None:
            rows = rows[self.range_start:self.range_end + 1]

        return FakeResult(rows, count=total if self.exact_count else None)

    def _matches(self, row: dict) -> bool:
        for column, value in self.filters:
            if row.get(column) != value:
                return False
        for column, value in self.gte_filters:
            if row.get(column) < value:
                return False
        return True


@pytest.fixture
def audit_surface_client():
    rows_by_table = {
        "audit_logs": [
            {
                "id": 2,
                "firm_id": "firm-a",
                "actor_type": "user",
                "actor_id": "ops-1",
                "action": "commitment.update",
                "entity_type": "commitment",
                "entity_id": "commitment-1",
                "before": {"amount": 100},
                "after": {"amount": 200},
                "metadata": {"source": "test"},
                "row_hash": "private-row-hash",
                "prior_hash": "private-prior-hash",
                "created_at": "2026-05-10T12:00:00Z",
            },
            {
                "id": 1,
                "firm_id": "firm-a",
                "actor_type": "system",
                "actor_id": None,
                "action": "commitment.create",
                "entity_type": "commitment",
                "entity_id": "commitment-1",
                "before": None,
                "after": {"amount": 100},
                "metadata": {},
                "row_hash": "private-row-hash",
                "prior_hash": "private-prior-hash",
                "created_at": "2026-05-09T12:00:00Z",
            },
            {
                "id": 3,
                "firm_id": "firm-b",
                "actor_type": "system",
                "actor_id": None,
                "action": "commitment.create",
                "entity_type": "commitment",
                "entity_id": "commitment-1",
                "before": None,
                "after": {},
                "metadata": {},
                "created_at": "2026-05-11T12:00:00Z",
            },
        ],
        "ai_invocations": [
            {
                "firm_id": "firm-a",
                "engine": "openai_vision",
                "task": "kyc_review",
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": 0.125,
                "latency_ms": 1000,
                "created_at": "2026-05-10T12:00:00Z",
            },
            {
                "firm_id": "firm-a",
                "engine": "openai_vision",
                "task": "kyc_review",
                "input_tokens": 200,
                "output_tokens": 100,
                "cost_usd": 0.375,
                "latency_ms": 2000,
                "created_at": "2026-05-11T12:00:00Z",
            },
            {
                "firm_id": "firm-a",
                "engine": "anthropic_claude",
                "task": "side_letter_review",
                "input_tokens": 25,
                "output_tokens": 10,
                "cost_usd": 0.025,
                "latency_ms": 500,
                "created_at": "2026-04-01T12:00:00Z",
            },
        ],
    }
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase(rows_by_table)

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("api.routes.audit_surface", None)
        audit_surface = importlib.import_module("api.routes.audit_surface")

        app = FastAPI()
        app.include_router(audit_surface.router)
        yield TestClient(app)


def test_entity_audit_returns_paginated_public_rows(audit_surface_client):
    response = audit_surface_client.get(
        "/audit/commitment/commitment-1?limit=1&offset=1",
        headers={"X-Firm-ID": "firm-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "rows": [
            {
                "id": 2,
                "actor_type": "user",
                "actor_id": "ops-1",
                "action": "commitment.update",
                "before": {"amount": 100},
                "after": {"amount": 200},
                "metadata": {"source": "test"},
                "created_at": "2026-05-10T12:00:00Z",
            }
        ],
        "total": 2,
    }


def test_firm_audit_summary_groups_actions(audit_surface_client):
    response = audit_surface_client.get(
        "/audit/firm/summary?since=2026-05-01",
        headers={"X-Firm-ID": "firm-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "since": "2026-05-01",
        "summary": [
            {"action": "commitment.update", "count": 1},
            {"action": "commitment.create", "count": 1},
        ],
    }


def test_ai_invocation_cost_summary_groups_costs_and_tokens(audit_surface_client):
    response = audit_surface_client.get(
        "/ai/invocations/cost-summary?since=2026-05-01",
        headers={"X-Firm-ID": "firm-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "since": "2026-05-01",
        "by_engine": [
            {
                "engine": "openai_vision",
                "task": "kyc_review",
                "call_count": 2,
                "total_cost_usd": 0.5,
                "total_tokens": 450,
                "avg_latency_ms": 1500.0,
            }
        ],
    }
