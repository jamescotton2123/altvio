import importlib
import sys
import types
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def table(self, name: str):
        return FakeQuery(name)


class FakeQuery:
    def __init__(self, table_name: str):
        self.table_name = table_name

    def select(self, _columns: str):
        return self

    def eq(self, _column: str, _value):
        return self

    def order(self, _column: str, desc: bool = False):
        return self

    def single(self):
        return self

    def execute(self):
        if self.table_name == "deals":
            return FakeResult(
                {
                    "id": "deal-1",
                    "offering_name": "Test Fund",
                    "target_raise": 1000000,
                    "status": "Active",
                    "firm_id": "firm-1",
                }
            )
        return FakeResult([])


def test_deal_hub_advisor_requires_advisor_email_header():
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase()

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("api.routes.deal_hub", None)
        deal_hub = importlib.import_module("api.routes.deal_hub")

    app = FastAPI()
    app.include_router(deal_hub.router, prefix="/deals")
    client = TestClient(app)

    response = client.get(
        "/deals/deal-1/hub?role=advisor",
        headers={"X-Firm-ID": "firm-1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "X-Advisor-Email header is required when role=advisor."
