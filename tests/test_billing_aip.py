import importlib
import sys
import types
from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self, funded_amount: Decimal):
        self.funded_amount = funded_amount
        self.billing_usage = []
        self.billing_invoices = []
        self._next_usage_id = 1
        self._next_invoice_id = 1

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.payload = None
        self.operation = "select"

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

    def update(self, payload: dict):
        self.operation = "update"
        self.payload = payload
        return self

    def execute(self):
        if self.operation == "insert":
            return self._insert()
        if self.operation == "update":
            return self._update()
        return FakeResult(self._select())

    def _select(self):
        if self.table_name == "commitments":
            if self._has_filter("firm_id"):
                return [
                    {
                        "id": "commitment-1",
                        "investor_id": "investor-1",
                        "investors": {"kyc_status": "Pending"},
                        "docusign_status": "Pending",
                        "funded_amount": str(self.db.funded_amount),
                    }
                ]
            return []

        if self.table_name == "billing_usage":
            return [row for row in self.db.billing_usage if self._matches(row)]

        if self.table_name == "billing_invoices":
            return [row for row in self.db.billing_invoices if self._matches(row)]

        return []

    def _insert(self):
        if self.table_name == "billing_usage":
            row = {"id": f"usage-{self.db._next_usage_id}", **self.payload}
            self.db._next_usage_id += 1
            self.db.billing_usage.append(row)
            return FakeResult([row])

        if self.table_name == "billing_invoices":
            row = {"id": f"invoice-{self.db._next_invoice_id}", **self.payload}
            self.db._next_invoice_id += 1
            self.db.billing_invoices.append(row)
            return FakeResult([row])

        return FakeResult([])

    def _update(self):
        rows = self._select()
        for row in rows:
            row.update(self.payload)
        return FakeResult(rows)

    def _has_filter(self, column: str) -> bool:
        return any(filter_column == column for filter_column, _ in self.filters)

    def _matches(self, row: dict) -> bool:
        return all(row.get(column) == value for column, value in self.filters)


def test_monthly_aip_billing_charges_one_annual_bps_over_twelve_periods():
    funded_amount = Decimal("1200000")
    fake_supabase = FakeSupabase(funded_amount=funded_amount)
    fake_database = types.ModuleType("core.database")
    fake_database.supabase = fake_supabase

    with patch.dict(sys.modules, {"core.database": fake_database}):
        sys.modules.pop("core.billing", None)
        billing = importlib.import_module("core.billing")

    firm_id = "firm-1"
    for month in range(1, 13):
        billing.materialize_billing_period(firm_id, f"2026-{month:02d}", granularity="monthly")

    billed_cents = sum(
        row["amount_cents"]
        for row in fake_supabase.billing_usage
        if row["event_type"] == "aip_bps_quarterly"
    )
    expected_cents = int(
        (funded_amount * (Decimal("1.5") / Decimal("10000")) * Decimal(100)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )

    assert billed_cents == expected_cents
