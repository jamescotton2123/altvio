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
        self.audit_logs = []
        self._next_audit_id = 1

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.order_column = None
        self.order_desc = False
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

    def order(self, column: str, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
        return self

    def limit(self, count: int):
        self.row_limit = count
        return self

    def execute(self):
        if self.operation == "insert":
            return self._insert()
        if self.operation == "update":
            return self._update()
        return FakeResult(self._select())

    def _select(self):
        if self.table_name != "audit_logs":
            return []

        rows = [row for row in self.db.audit_logs if self._matches(row)]
        if self.order_column:
            rows = sorted(
                rows,
                key=lambda row: row[self.order_column],
                reverse=self.order_desc,
            )
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return [row.copy() for row in rows]

    def _insert(self):
        if self.table_name != "audit_logs":
            return FakeResult([])

        row = {"id": self.db._next_audit_id, **self.payload}
        self.db._next_audit_id += 1
        self.db.audit_logs.append(row)
        return FakeResult([row.copy()])

    def _update(self):
        rows = []
        if self.table_name == "audit_logs":
            for row in self.db.audit_logs:
                if self._matches(row):
                    row.update(self.payload)
                    rows.append(row.copy())
        return FakeResult(rows)

    def _matches(self, row: dict) -> bool:
        return all(row.get(column) == value for column, value in self.filters)


@pytest.fixture
def audit_context():
    fake_supabase = FakeSupabase()
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    with patch.dict(
            sys.modules,
            {"core.database": fake_database},
    ):
        sys.modules.pop("core.audit", None)
        sys.modules.pop("api.routes.audit", None)

        audit = importlib.import_module("core.audit")
        audit_route = importlib.import_module("api.routes.audit")

        app = FastAPI()
        app.include_router(audit_route.router, prefix="/audit")
        yield types.SimpleNamespace(
            audit=audit,
            client=TestClient(app),
            fake_supabase=fake_supabase,
        )


def test_verify_detects_corrupted_second_row(audit_context, firm_id_a):
    entity_id = "10000000-0000-0000-0000-000000000001"

    audit_context.audit.log_audit(
        firm_id=firm_id_a,
        actor_type="system",
        actor_id=None,
        action="first",
        entity_type="investor",
        entity_id=entity_id,
        metadata={"sequence": 1},
    )
    second_id = audit_context.audit.log_audit(
        firm_id=firm_id_a,
        actor_type="system",
        actor_id=None,
        action="second",
        entity_type="investor",
        entity_id=entity_id,
        metadata={"sequence": 2},
    )
    audit_context.audit.log_audit(
        firm_id=firm_id_a,
        actor_type="system",
        actor_id=None,
        action="third",
        entity_type="investor",
        entity_id=entity_id,
        metadata={"sequence": 3},
    )

    response = audit_context.client.get(f"/audit/verify?firm_id={firm_id_a}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "rows_checked": 3}

    audit_context.fake_supabase.table("audit_logs").update(
        {"metadata": {"sequence": "corrupted"}}
    ).eq("id", second_id).execute()

    response = audit_context.client.get(f"/audit/verify?firm_id={firm_id_a}")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "first_bad_row_id": second_id}
