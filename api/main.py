"""
Altvio — FastAPI application entry point.
Registers all route modules and starts the follow-up scheduler.
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from api.routes import (
    advisor_portal,
    audit,
    audit_surface,
    billing,
    change_loi,
    client_associate_portal,
    commitments,
    deal_hub,
    distribution_events,
    docusign_webhook,
    email_templates_crud,
    exec_dashboard,
    firm_settings,
    followup,
    imports_exports,
    intake,
    investors,
    kyc_webhook,
    loi,
    ops_todos,
    portal,
    query,
    trader_portal,
    transfers,
    wire_templates,
)
from core.auth import intake_key_limiter
from core.logging_config import configure_logging


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    from core.followup_scheduler import start_scheduler
    from core.mailbox_subscription_manager import bootstrap_all_mailbox_subscriptions

    scheduler = start_scheduler()
    try:
        bootstrap_all_mailbox_subscriptions()
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Graph mailbox subscription bootstrap failed at startup."
        )
    yield
    scheduler.shutdown()


app = FastAPI(
    title="Altvio",
    description="Institutional Alternative Investment Operations Platform",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = intake_key_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RequestIDMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "https://altvio.app",
        "https://www.altvio.app",
        "https://usealtvio.app",
    ],
    # Next.js dev server also binds LAN IPs (e.g. http://192.168.x.x:3000).
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(intake.router, prefix="/intake", tags=["Intake"])
app.include_router(docusign_webhook.router, prefix="/docusign", tags=["DocuSign"])
app.include_router(kyc_webhook.router, prefix="/kyc", tags=["KYC"])
app.include_router(deal_hub.router, prefix="/deals", tags=["Deal Hub"])
app.include_router(commitments.router, prefix="/commitments", tags=["Commitments"])
app.include_router(followup.router, prefix="/followup", tags=["Follow-up"])
app.include_router(wire_templates.router, prefix="/wire", tags=["Wire Templates"])
app.include_router(loi.router, prefix="/loi", tags=["LOI"])
app.include_router(transfers.router, prefix="/transfers", tags=["Transfers of Interest"])
app.include_router(investors.router, prefix="/investors", tags=["Investors"])
app.include_router(change_loi.router, tags=["Change LOI"])
app.include_router(portal.router, prefix="/portal", tags=["Investor Portal"])
app.include_router(ops_todos.router, prefix="/ops/todos", tags=["Ops To-Do"])
app.include_router(exec_dashboard.router, prefix="/exec", tags=["Executive Command Center"])
app.include_router(advisor_portal.router, prefix="/advisor", tags=["Advisor Portal"])
app.include_router(firm_settings.router, prefix="/firm/settings", tags=["Firm Settings"])
app.include_router(email_templates_crud.router, prefix="/firm/templates", tags=["Email Templates"])
app.include_router(trader_portal.router, prefix="/trader", tags=["Trader Portal"])
app.include_router(client_associate_portal.router, prefix="/client-associate", tags=["Client Associate Portal"])
app.include_router(billing.router, prefix="/billing", tags=["Billing"])
app.include_router(query.router, prefix="/query", tags=["NL Query"])
app.include_router(audit.router, prefix="/audit", tags=["Audit"])
app.include_router(audit_surface.router, tags=["Audit Surface"])
app.include_router(imports_exports.router, tags=["Imports & Exports"])
app.include_router(distribution_events.router, tags=["Distribution Events"])


@app.get("/health")
def health_check():
    return {"status": "ok", "platform": "Altvio"}
