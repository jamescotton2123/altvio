import importlib
import sys
import types
from unittest.mock import patch

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

VALID_KEY = "pop_live_test_12345678"
WRONG_KEY = "pop_live_wrong_12345678"
FIRM_ID = "firm-1"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self):
        self.key_hash = bcrypt.hashpw(VALID_KEY.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        self.settings = {"firm_id": FIRM_ID, "portal_link_expiry_days": 30}

    def table(self, name: str):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters = []

    def select(self, _columns: str):
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def single(self):
        return self

    def execute(self):
        if self.table_name == "firm_intake_keys" and self._has_filter("key_last8", VALID_KEY[-8:]):
            return FakeResult(
                [
                    {
                        "firm_id": FIRM_ID,
                        "key_hash": self.db.key_hash,
                        "revoked_at": None,
                    }
                ]
            )

        if self.table_name == "firm_settings" and self._has_filter("firm_id", FIRM_ID):
            return FakeResult(self.db.settings.copy())

        if self.table_name == "deals" and self._has_filter("firm_id", FIRM_ID):
            return FakeResult({"id": "deal-1", "offering_name": "Test Fund", "status": "Active"})

        return FakeResult(None)

    def _has_filter(self, column: str, value) -> bool:
        return any(filter_column == column and filter_value == value for filter_column, filter_value in self.filters)


@pytest.fixture
def intake_context():
    onboardings = []
    portal_tokens = []

    def run_onboarding(**kwargs):
        onboardings.append(kwargs)
        return {"investor_id": "investor-1", "commitment_id": "commitment-1"}

    def generate_portal_token(**kwargs):
        portal_tokens.append(kwargs)
        return {"portal_token": "token-1"}

    fake_database = types.ModuleType("core.database")
    fake_database.supabase = FakeSupabase()

    fake_ai_parser = types.ModuleType("core.ai_parser")
    fake_ai_parser.parse_email = lambda _raw_text: {}
    fake_ai_parser.parse_form_submission = lambda payload: payload

    fake_onboarding = types.ModuleType("core.onboarding")
    fake_onboarding.run_onboarding = run_onboarding

    fake_portal = types.ModuleType("core.portal")
    fake_portal.validate_portal_token = lambda _token: None
    fake_portal.validate_kyc_token = lambda _token: None
    fake_portal.assemble_portal_data = lambda *_args, **_kwargs: {}
    fake_portal.assemble_kyc_portal_data = lambda *_args, **_kwargs: {}
    fake_portal.generate_portal_token = generate_portal_token
    fake_portal.request_portal_access = lambda *_args, **_kwargs: False

    with patch.dict(
            sys.modules,
            {
                "core.database": fake_database,
                "core.ai_parser": fake_ai_parser,
                "core.onboarding": fake_onboarding,
                "core.portal": fake_portal,
            },
    ):
        sys.modules.pop("core.auth", None)
        sys.modules.pop("api.routes.intake", None)
        sys.modules.pop("api.routes.portal", None)

        auth = importlib.import_module("core.auth")
        intake = importlib.import_module("api.routes.intake")
        portal = importlib.import_module("api.routes.portal")

        app = FastAPI()
        app.state.limiter = auth.intake_key_limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.include_router(intake.router, prefix="/intake")
        app.include_router(portal.router, prefix="/portal")
        yield types.SimpleNamespace(
            client=TestClient(app),
            onboardings=onboardings,
            portal_tokens=portal_tokens,
        )
        sys.modules.pop("core.auth", None)
        sys.modules.pop("api.routes.intake", None)
        sys.modules.pop("api.routes.portal", None)


def _form_payload(firm_id=None):
    payload = {
        "investor_name": "Test Investor",
        "fund_name": "Test Fund",
        "committed_amount": 100000,
    }
    if firm_id:
        payload["firm_id"] = firm_id
    return payload


def test_missing_key_returns_401(intake_context):
    response = intake_context.client.post("/intake/form", json=_form_payload())

    assert response.status_code == 401


def test_wrong_key_returns_401(intake_context):
    response = intake_context.client.post(
        "/intake/form",
        json=_form_payload(),
        headers={"X-Intake-Key": WRONG_KEY},
    )

    assert response.status_code == 401


def test_valid_key_resolves_correct_firm_id(intake_context):
    intake_response = intake_context.client.post(
        "/intake/form",
        json=_form_payload(firm_id=FIRM_ID),
        headers={"X-Intake-Key": VALID_KEY},
    )
    portal_response = intake_context.client.post(
        "/portal/generate-link",
        json={
            "firm_id": FIRM_ID,
            "investor_id": "investor-1",
            "commitment_id": "commitment-1",
        },
        headers={"X-Intake-Key": VALID_KEY},
    )

    assert intake_response.status_code == 200
    assert portal_response.status_code == 200
    assert intake_context.onboardings[0]["firm_id"] == FIRM_ID
    assert intake_context.portal_tokens[0]["firm_id"] == FIRM_ID


def test_body_firm_id_mismatch_returns_422(intake_context):
    response = intake_context.client.post(
        "/intake/form",
        json=_form_payload(firm_id="firm-2"),
        headers={"X-Intake-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert intake_context.onboardings == []


def test_rate_limit_fires_after_60_requests(intake_context):
    for _ in range(60):
        response = intake_context.client.post(
            "/intake/form",
            json=_form_payload(),
            headers={"X-Intake-Key": VALID_KEY},
        )
        assert response.status_code == 200

    response = intake_context.client.post(
        "/intake/form",
        json=_form_payload(),
        headers={"X-Intake-Key": VALID_KEY},
    )

    assert response.status_code == 429
