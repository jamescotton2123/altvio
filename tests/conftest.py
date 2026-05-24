import pytest


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, list[dict]] | None = None):
        self.rows_by_table = rows_by_table or {}
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
        self.row_limit = None
        self.order_column = None
        self.order_desc = False

    def select(self, _columns: str):
        self.operation = "select"
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def single(self):
        self.return_single = True
        return self

    def limit(self, count: int):
        self.row_limit = count
        return self

    def order(self, column: str, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
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
        self.db.query_log.append((self.table_name, list(self.filters), self.operation))
        if self.operation == "insert":
            return self._insert()
        if self.operation == "update":
            return self._update()

        rows = self._select()
        if self.order_column:
            rows = sorted(
                rows,
                key=lambda row: row[self.order_column],
                reverse=self.order_desc,
            )
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        if self.return_single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)

    def _select(self):
        return [
            row
            for row in self.db.rows_by_table.get(self.table_name, [])
            if all(row.get(column) == value for column, value in self.filters)
        ]

    def _insert(self):
        row = self.payload.copy()
        self.db.rows_by_table.setdefault(self.table_name, []).append(row)
        return FakeResult([row])

    def _update(self):
        rows = self._select()
        for row in rows:
            row.update(self.payload)
        return FakeResult(rows)


@pytest.fixture
def fake_supabase():
    return FakeSupabase()


@pytest.fixture
def firm_id_a():
    return "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def firm_id_b():
    return "00000000-0000-0000-0000-000000000002"
