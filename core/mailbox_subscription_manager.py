"""
Bootstrap and renew Microsoft Graph mailbox subscriptions for firm ops inboxes.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Optional

from core.database import supabase
from core.graph_client import subscribe_to_mailbox

logger = logging.getLogger(__name__)

RENEWAL_INTERVAL_HOURS = 12
SUBSCRIPTION_LIFETIME_HOURS = 4230  # ~176 days (Graph max for mail)


def _notification_url() -> Optional[str]:
    base = (os.environ.get("API_PUBLIC_BASE_URL") or os.environ.get("PUBLIC_API_URL") or "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/intake/email"


def ensure_mailbox_subscription(settings: dict) -> Optional[str]:
    """
    Create or renew a Graph subscription for the firm's ops_mailbox.
    Returns subscription id when successful.
    """
    firm_id = settings.get("firm_id")
    ops_mailbox = (settings.get("ops_mailbox") or "").strip()
    if not ops_mailbox:
        logger.debug("Skipping Graph subscription for firm %s: no ops_mailbox.", firm_id)
        return None

    notification_url = _notification_url()
    if not notification_url:
        logger.warning(
            "Skipping Graph subscription for firm %s: set API_PUBLIC_BASE_URL.",
            firm_id,
        )
        return None

    settings_with_firm = {**settings, "firm_id": firm_id}
    subscription_id = subscribe_to_mailbox(settings_with_firm, notification_url)
    expires_at = (
        datetime.datetime.utcnow() + datetime.timedelta(hours=SUBSCRIPTION_LIFETIME_HOURS)
    ).isoformat() + "Z"

    supabase.table("firm_settings").update({
        "graph_subscription_id": subscription_id,
        "graph_subscription_expires_at": expires_at,
    }).eq("firm_id", firm_id).execute()

    logger.info(
        "Graph mail subscription active for firm %s mailbox %s (id=%s).",
        firm_id,
        ops_mailbox,
        subscription_id,
    )
    return subscription_id


def renew_mailbox_subscriptions() -> dict:
    """Renew subscriptions for all firms with ops_mailbox configured."""
    firms = (
        supabase.table("firm_settings")
        .select("*")
        .not_.is_("ops_mailbox", "null")
        .execute()
        .data
        or []
    )
    renewed = 0
    skipped = 0
    errors: list[str] = []

    for settings in firms:
        mailbox = (settings.get("ops_mailbox") or "").strip()
        if not mailbox:
            skipped += 1
            continue
        try:
            ensure_mailbox_subscription(settings)
            renewed += 1
        except Exception as exc:
            logger.error(
                "Failed Graph subscription for firm %s: %s",
                settings.get("firm_id"),
                exc,
            )
            errors.append(str(settings.get("firm_id")))

    return {"renewed": renewed, "skipped": skipped, "errors": errors}


def bootstrap_all_mailbox_subscriptions() -> dict:
    """Called at app startup to register subscriptions."""
    return renew_mailbox_subscriptions()
