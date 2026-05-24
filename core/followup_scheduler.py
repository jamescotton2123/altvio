"""
Automated follow-up scheduler using APScheduler.
Runs daily at 08:00:
  1. KYC follow-up pings — investors with kyc_status='Pending' past the firm's threshold
  2. Wire follow-up pings — signed sub docs but wire still pending
  3. Ops email digest — expiring/expired placement upfront fee terms (if notify_ops_fee_expiry)
  4. Trader desk digest + Client Associate emails — private-wealth liquidation tickets (if notify_trader_liquidation_digest)

Runs weekly Mondays at 08:00 (opt-in per firm):
  5. Unconfirmed physical mailings — statement_mailings with mailed_date ≥30 days ago and not confirmed
     (only when firm_settings.notify_statement_mailing_unconfirmed is true)

Runs monthly on the 1st at 06:00:
  6. Billing materialization — previous calendar month usage + draft invoice (all firms in firm_settings)

Runs weekly Mondays at 07:00:
  7. Advisor desk health reports for Executive Command Center (firm brief + per-desk metrics)

For each overdue item, sends an advisor ping asking if they want to send a follow-up.
The advisor approves via a link → triggers the follow-up email to the investor.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.fee_expiry_digest import send_fee_expiry_ops_digest
from core.followup_tokens import mint_followup_token

logger = logging.getLogger(__name__)


def _days_since(dt_str: str) -> int:
    """Calculate business days since a given ISO timestamp string."""
    if not dt_str:
        return 0
    created = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - created).days


def check_kyc_overdue(firm_id: str, settings: dict) -> None:
    """
    Find all investors in a firm with kyc_status='Pending' past threshold.
    Send an advisor ping for each.
    """
    from core.graph_client import send_email

    threshold_days = settings.get("kyc_followup_days", 5)
    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    base_url = os.environ.get("PLATFORM_BASE_URL", "https://app.pivotops.pro")

    investors = (
        supabase.table("investors")
        .select("id, entity_name, advisor_email, primary_email, sharepoint_link, created_at")
        .eq("firm_id", firm_id)
        .eq("kyc_status", "Pending")
        .execute()
        .data
    )

    for investor in investors:
        days = _days_since(investor["created_at"])
        if days < threshold_days:
            continue

        advisor_email = investor.get("advisor_email")
        if not advisor_email:
            continue

        token = mint_followup_token(
            firm_id=firm_id,
            type="kyc",
            investor_id=investor["id"],
        )
        approve_url = f"{base_url}/followup/{token}/approve"

        subject = f"Follow-up Needed: KYC Outstanding — {investor['entity_name']}"
        body = (
            f"Hi,\n\n"
            f"{investor['entity_name']} has not yet submitted their KYC documentation. "
            f"Their documents have been outstanding for {days} day(s).\n\n"
            f"Would you like to send them a follow-up reminder?\n\n"
            f"  ✓ Yes, send the follow-up: {approve_url}\n\n"
            f"If you do not want a follow-up sent, no action is needed.\n\n"
            f"— Altvio Platform"
        )

        send_email(
            settings=settings,
            to=advisor_email,
            cc=[ops_mailbox] if ops_mailbox else [],
            subject=subject,
            body=body,
        )
        logger.info("KYC ping sent to %s for %s (%sd outstanding).", advisor_email, investor["entity_name"], days)


def check_wire_overdue(firm_id: str, settings: dict) -> None:
    """
    Find all commitments with wire_status='Awaiting Funds' and signed sub docs
    past the firm's wire follow-up threshold. Send an advisor ping for each.
    """
    from core.graph_client import send_email

    threshold_days = settings.get("wire_followup_days", 3)
    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    base_url = os.environ.get("PLATFORM_BASE_URL", "https://app.pivotops.pro")

    commitments = (
        supabase.table("commitments")
        .select("id, investor_id, committed_amount, created_at, investors(entity_name, advisor_email, primary_email), deals(offering_name, wire_instructions)")
        .eq("firm_id", firm_id)
        .eq("wire_status", "Awaiting Funds")
        .in_("docusign_status", sorted(SIGNED_STATES))
        .eq("status", "Active")
        .execute()
        .data
    )

    for commitment in commitments:
        days = _days_since(commitment["created_at"])
        if days < threshold_days:
            continue

        investor = commitment.get("investors", {})
        deal = commitment.get("deals", {})
        advisor_email = investor.get("advisor_email")
        entity_name = investor.get("entity_name", "Unknown Investor")
        fund_name = deal.get("offering_name", "the fund")

        if not advisor_email:
            continue

        token = mint_followup_token(
            firm_id=firm_id,
            type="wire",
            commitment_id=commitment["id"],
        )
        approve_url = f"{base_url}/followup/{token}/approve"

        subject = f"Follow-up Needed: Wire Pending — {entity_name} / {fund_name}"
        body = (
            f"Hi,\n\n"
            f"{entity_name}'s subscription documents for {fund_name} have been signed, "
            f"but their wire of ${commitment['committed_amount']:,.2f} has not yet been received. "
            f"This has been outstanding for {days} day(s).\n\n"
            f"Would you like to send them a wire reminder?\n\n"
            f"  ✓ Yes, send the reminder: {approve_url}\n\n"
            f"If you do not want a reminder sent, no action is needed.\n\n"
            f"— Altvio Platform"
        )

        send_email(
            settings=settings,
            to=advisor_email,
            cc=[ops_mailbox] if ops_mailbox else [],
            subject=subject,
            body=body,
        )
        logger.info("Wire ping sent to %s for %s (%sd outstanding).", advisor_email, entity_name, days)


def send_kyc_followup(investor_id: str, firm_id: str, settings: dict) -> None:
    """
    Send the actual KYC follow-up email to the investor.
    Triggered when the advisor approves via the ping link.
    """
    from core.graph_client import send_email
    from core.kyc_templates import build_kyc_followup_email

    investor = (
        supabase.table("investors")
        .select("entity_name, primary_email, advisor_email, sharepoint_link, created_at")
        .eq("id", investor_id)
        .single()
        .execute()
        .data
    )

    days = _days_since(investor["created_at"])
    email = build_kyc_followup_email(
        entity_name=investor["entity_name"],
        offering_name="your pending fund investment",
        sharepoint_upload_link=investor.get("sharepoint_link", ""),
        days_outstanding=days,
    )

    send_email(
        settings=settings,
        to=investor["primary_email"],
        cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
        subject=email["subject"],
        body=email["body"],
    )
    logger.info("KYC follow-up sent to %s", investor["primary_email"])


def send_wire_followup(commitment_id: str, firm_id: str, settings: dict) -> None:
    """
    Send the actual wire follow-up email to the investor.
    Triggered when the advisor approves via the ping link.
    """
    from core.email_templates import build_wire_followup_email
    from core.graph_client import send_email

    commitment = (
        supabase.table("commitments")
        .select("committed_amount, created_at, deal_id, firm_id, investors(entity_name, primary_email, advisor_email), deals(offering_name, wire_instructions)")
        .eq("id", commitment_id)
        .single()
        .execute()
        .data
    )

    investor = commitment.get("investors", {})
    deal = commitment.get("deals", {})
    days = _days_since(commitment["created_at"])

    from core.deal_fees import compute_commitment_wire_breakdown
    wire_breakdown = compute_commitment_wire_breakdown(
        committed_amount=float(commitment.get("committed_amount") or 0),
        deal_id=commitment["deal_id"],
        firm_id=firm_id,
    )

    email = build_wire_followup_email(
        entity_name=investor.get("entity_name", ""),
        offering_name=deal.get("offering_name", ""),
        committed_amount=commitment["committed_amount"],
        wire_instructions=deal.get("wire_instructions", "Please contact operations for wire instructions."),
        days_outstanding=days,
        ops_contact_email=settings.get("ops_mailbox"),
        wire_breakdown=wire_breakdown,
        firm_id=firm_id,
    )

    send_email(
        settings=settings,
        to=investor["primary_email"],
        cc=[investor["advisor_email"]] if investor.get("advisor_email") else [],
        subject=email["subject"],
        body=email["body"],
    )
    logger.info("Wire follow-up sent to %s", investor["primary_email"])


def check_unconfirmed_mailings(firm_id: str, settings: dict) -> None:
    """
    Alert ops when a physical mailing was sent ≥30 days ago but receipt was not confirmed.
    Uses firm_settings.ops_mailbox (via settings + OPS_MAILBOX env fallback).

    Skipped unless firm_settings.notify_statement_mailing_unconfirmed is true (opt-in).
    """
    from core.graph_client import send_email

    if not settings.get("notify_statement_mailing_unconfirmed"):
        return

    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    if not ops_mailbox:
        logger.warning("Unconfirmed mailings skipped for firm %s: no ops_mailbox.", firm_id)
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()

    mailings = (
        supabase.table("statement_mailings")
        .select("id, investor_id, document_type, period, mailed_date, tracking_number")
        .eq("firm_id", firm_id)
        .eq("confirmed_received", False)
        .lte("mailed_date", cutoff.isoformat())
        .execute()
        .data
    ) or []

    mailings = [m for m in mailings if m.get("mailed_date")]
    if not mailings:
        return

    inv_ids = list({m["investor_id"] for m in mailings})
    invs = (
        supabase.table("investors")
        .select("id, entity_name")
        .in_("id", inv_ids)
        .eq("firm_id", firm_id)
        .execute()
        .data
    ) or []
    inv_map = {str(i["id"]): (i.get("entity_name") or "Unknown investor") for i in invs}

    for m in mailings:
        entity_name = inv_map.get(str(m["investor_id"]), "Unknown investor")
        mailed = m.get("mailed_date")
        tracking = m.get("tracking_number") or "(none)"
        subject = f"Unconfirmed mailing (30d+): {entity_name} — {m.get('document_type', '')}"
        body = (
            f"A physical mailing has not been marked as received after 30+ days.\n\n"
            f"Investor:     {entity_name}\n"
            f"Document:     {m.get('document_type', '')}\n"
            f"Period:       {m.get('period', '')}\n"
            f"Mailed date:  {mailed}\n"
            f"Tracking:     {tracking}\n"
            f"Mailing ID:   {m.get('id')}\n\n"
            f"Please confirm receipt in Altvio (Investor → Mailings) or follow up with the investor.\n\n"
            f"— Altvio Platform"
        )
        send_email(settings=settings, to=ops_mailbox, subject=subject, body=body)
        logger.info("Unconfirmed mailing alert sent for %s (mailing %s).", entity_name, m.get("id"))


def _previous_calendar_month_period(now: datetime | None = None) -> str:
    """YYYY-MM for the month before `now` (UTC). On May 1 → 'YYYY-04'."""
    now = now or datetime.now(timezone.utc)
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this_month - timedelta(days=1)
    return f"{last_prev.year}-{last_prev.month:02d}"


def _format_cents_dollars(cents: int | None) -> str:
    return f"${(int(cents or 0) / 100):,.2f}"


def run_monthly_billing(firm_id: str) -> dict:
    """
    Materialize billing_usage + draft billing_invoices for the prior calendar month.
    Emails ops_mailbox a summary when configured.
    """
    from core.billing import materialize_billing_period
    from core.graph_client import send_email

    billing_period = _previous_calendar_month_period()
    result = materialize_billing_period(firm_id, billing_period, granularity="monthly")

    settings = (
        supabase.table("firm_settings")
        .select("*")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    ops_mailbox = (settings or {}).get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    if ops_mailbox:
        body = (
            f"Billing period {billing_period} has been materialized.\n\n"
            f"Onboarding usage rows created: {result.get('onboarding_rows_created', 0)}\n"
            f"AIP amount (monthly snapshot): {_format_cents_dollars(result.get('aip_amount_cents'))}\n"
            f"Draft invoice total: {_format_cents_dollars(result.get('invoice_total_cents'))}\n"
        )
        if result.get("invoice_id"):
            body += f"\nInvoice ID: {result['invoice_id']}\n"
        body += "\n— Altvio Platform"
        send_email(
            settings=settings or {},
            to=ops_mailbox,
            subject=f"Altvio — Billing period {billing_period} materialized",
            body=body,
        )
        logger.info("Billing summary emailed to %s for firm %s (%s).", ops_mailbox, firm_id, billing_period)
    else:
        logger.info("Billing materialized for firm %s (%s); no ops_mailbox for email.", firm_id, billing_period)

    return result


def start_scheduler() -> BackgroundScheduler:
    """
    Start the APScheduler background scheduler.
    Runs follow-up checks daily at 8:00 AM for all active firms.
    Call this at FastAPI app startup.
    """
    scheduler = BackgroundScheduler()

    def daily_followup_run():
        firms = supabase.table("firms").select("id").eq("status", "active").execute().data or []
        for firm_row in firms:
            firm_id = firm_row["id"]
            settings = (
                supabase.table("firm_settings")
                .select("*")
                .eq("firm_id", firm_id)
                .single()
                .execute()
                .data
            )
            if not settings:
                continue
            try:
                check_kyc_overdue(firm_id, settings)
                check_wire_overdue(firm_id, settings)
                send_fee_expiry_ops_digest(firm_id, settings)
            except Exception as e:
                logger.error("Follow-up run failed for firm %s: %s", firm_id, e)
        try:
            from core.trader_liquidation_digest import send_all_firm_trader_digests

            send_all_firm_trader_digests()
        except Exception as e:
            logger.error("Trader liquidation digests failed: %s", e)

    scheduler.add_job(
        daily_followup_run,
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_followups",
        replace_existing=True,
    )

    def weekly_unconfirmed_mailings_run():
        firms = supabase.table("firms").select("id").eq("status", "active").execute().data or []
        for firm_row in firms:
            firm_id = firm_row["id"]
            settings = (
                supabase.table("firm_settings")
                .select("*")
                .eq("firm_id", firm_id)
                .single()
                .execute()
                .data
            )
            if not settings:
                continue
            try:
                check_unconfirmed_mailings(firm_id, settings)
            except Exception as e:
                logger.error("Unconfirmed mailings failed for firm %s: %s", firm_id, e)

    scheduler.add_job(
        weekly_unconfirmed_mailings_run,
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_unconfirmed_mailings",
        replace_existing=True,
    )

    def monthly_billing_run():
        firm_rows = supabase.table("firm_settings").select("firm_id").execute().data or []
        for row in firm_rows:
            firm_id = row.get("firm_id")
            if not firm_id:
                continue
            try:
                run_monthly_billing(firm_id)
            except Exception as e:
                logger.error("Monthly billing materialize failed for firm %s: %s", firm_id, e)

    scheduler.add_job(
        monthly_billing_run,
        trigger=CronTrigger(day=1, hour=6, minute=0),
        id="monthly_billing_materialize",
        replace_existing=True,
    )

    def weekly_advisor_insights_run():
        firms = supabase.table("firms").select("id").eq("status", "active").execute().data or []
        for firm_row in firms:
            firm_id = firm_row["id"]
            try:
                from core.advisor_insights import generate_advisor_insights

                generate_advisor_insights(firm_id)
            except Exception as e:
                logger.error("Weekly advisor insights failed for firm %s: %s", firm_id, e)

    scheduler.add_job(
        weekly_advisor_insights_run,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="weekly_advisor_insights",
        replace_existing=True,
    )

    def renew_graph_mail_subscriptions():
        try:
            from core.mailbox_subscription_manager import renew_mailbox_subscriptions

            renew_mailbox_subscriptions()
        except Exception as e:
            logger.error("Graph mailbox subscription renewal failed: %s", e)

    scheduler.add_job(
        renew_graph_mail_subscriptions,
        trigger="interval",
        hours=12,
        id="renew_graph_mail_subscriptions",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: daily 08:00; advisor insights Mon 07:00; "
        "unconfirmed mailings Mon 08:00; billing 1st 06:00; Graph subs every 12h."
    )
    return scheduler
