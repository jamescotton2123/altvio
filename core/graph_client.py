"""
Microsoft Graph API client.
Handles all Microsoft 365 interactions:
  - Authenticated Graph API session via MSAL client credentials
  - SharePoint KYC folder provisioning + shareable link generation
  - Saving documents to SharePoint investor folders
  - Sending outbound emails via the ops mailbox
  - Subscribing to new emails on the ops mailbox (change notifications)
"""

import os
import secrets
from typing import Optional

import msal
import requests

from core.http_retry import REQUEST_TIMEOUT_SECONDS, request_with_retry


def _get_access_token(settings: dict) -> str:
    """Obtain a Microsoft Graph access token using client credentials flow."""
    tenant_id = settings.get("azure_tenant_id") or os.environ.get("AZURE_TENANT_ID")
    client_id = settings.get("azure_client_id") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = settings.get("azure_client_secret") or os.environ.get("AZURE_CLIENT_SECRET")

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

    if "access_token" not in result:
        raise RuntimeError(f"Failed to acquire Graph token: {result.get('error_description')}")

    return result["access_token"]


def _graph_headers(settings: dict) -> dict:
    token = _get_access_token(settings)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def build_sp_document_filename(
    template: str | None,
    entity_name: str,
    fund_name: str,
    suffix: str,
) -> str:
    """
    Build a SharePoint-safe upload filename from firm_settings.file_naming_template.

    Format placeholders: {entity_name}, {fund_name}, {suffix} (e.g. SignedDocs.pdf,
    LOI_Signed.pdf, TOI_Signed.pdf). Extra keyword args to str.format are ignored.

    If template is missing/blank, uses '{entity_name}_{fund_name}_{suffix}'.
    Unknown placeholders in a custom template fall back to the same default.
    """
    tpl = (template or "").strip() or "{entity_name}_{fund_name}_{suffix}"
    entity_short = (entity_name or "Investor")[:35]
    fund_safe = (fund_name or "Fund").strip() or "Fund"
    try:
        name = tpl.format(entity_name=entity_short, fund_name=fund_safe, suffix=suffix)
    except (KeyError, ValueError, IndexError):
        name = f"{entity_short}_{fund_safe}_{suffix}"
    return name.replace(" ", "_")


def create_kyc_folder(settings: dict, entity_name: str, fund_name: str) -> dict:
    """
    Create a KYC upload folder in SharePoint for an investor + fund combination.
    Returns { "folder_id": str, "sharepoint_link": str }
    """
    site_id = settings.get("sharepoint_site_id") or os.environ.get("SHAREPOINT_SITE_ID")
    drive_id = settings.get("sharepoint_drive_id") or os.environ.get("SHAREPOINT_DRIVE_ID")
    parent_folder = settings.get("sharepoint_kyc_root") or os.environ.get("SHAREPOINT_KYC_ROOT", "KYC Documents")

    headers = _graph_headers(settings)
    folder_name = f"{entity_name} - {fund_name}"

    # Create the folder
    create_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/root:/{parent_folder}:/children"
    create_resp = request_with_retry(
        requests.post,
        create_url,
        headers=headers,
        json={"name": folder_name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    create_resp.raise_for_status()
    folder = create_resp.json()
    folder_id = folder["id"]

    # Generate a shareable upload link
    link_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/items/{folder_id}/createLink"
    link_resp = request_with_retry(
        requests.post,
        link_url,
        headers=headers,
        json={"type": "edit", "scope": "anonymous"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    link_resp.raise_for_status()
    sharepoint_link = link_resp.json()["link"]["webUrl"]

    return {"folder_id": folder_id, "sharepoint_link": sharepoint_link}


def save_document_to_folder(
    settings: dict,
    folder_id: str,
    filename: str,
    file_bytes: bytes,
) -> str:
    """
    Upload a document (e.g. signed PDF) to an investor's SharePoint folder.
    Returns the web URL of the uploaded file.
    """
    site_id = settings.get("sharepoint_site_id") or os.environ.get("SHAREPOINT_SITE_ID")
    drive_id = settings.get("sharepoint_drive_id") or os.environ.get("SHAREPOINT_DRIVE_ID")
    token = _get_access_token(settings)

    upload_url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}"
        f"/items/{folder_id}:/{filename}:/content"
    )
    upload_resp = request_with_retry(
        requests.put,
        upload_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf"},
        data=file_bytes,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    upload_resp.raise_for_status()
    return upload_resp.json().get("webUrl", "")


def send_email(
    settings: dict,
    to: str,
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    attachments: Optional[list[dict]] = None,
) -> None:
    """
    Send an email via the ops mailbox using Microsoft Graph.
    attachments: list of { "name": str, "content_bytes": bytes }
    """
    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    headers = _graph_headers(settings)

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    }

    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc if addr]

    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["name"],
                "contentBytes": __import__("base64").b64encode(att["content_bytes"]).decode(),
            }
            for att in attachments
        ]

    send_url = f"https://graph.microsoft.com/v1.0/users/{ops_mailbox}/sendMail"
    resp = request_with_retry(
        requests.post,
        send_url,
        headers=headers,
        json={"message": message, "saveToSentItems": True},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def subscribe_to_mailbox(settings: dict, notification_url: str) -> str:
    """
    Register a Microsoft Graph change notification subscription on the ops mailbox.
    Fires a POST to notification_url whenever a new email arrives.
    Returns the subscription_id.
    """
    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    headers = _graph_headers(settings)
    client_state = secrets.token_urlsafe(32)

    import datetime
    expiry = (datetime.datetime.utcnow() + datetime.timedelta(hours=4230)).strftime(
        "%Y-%m-%dT%H:%M:%S.0000000Z"
    )

    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": f"users/{ops_mailbox}/mailFolders/Inbox/messages",
        "expirationDateTime": expiry,
        "clientState": client_state,
    }

    resp = request_with_retry(
        requests.post,
        "https://graph.microsoft.com/v1.0/subscriptions",
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    from core.database import supabase

    update = supabase.table("firm_settings").update(
        {"graph_subscription_client_state": client_state}
    )
    if settings.get("firm_id"):
        update = update.eq("firm_id", settings["firm_id"])
    else:
        update = update.eq("ops_mailbox", ops_mailbox)
    update.execute()

    return resp.json()["id"]


def get_email_body(settings: dict, message_id: str) -> str:
    """Fetch the full body of an email by message ID from the ops mailbox."""
    return get_email_message(settings, message_id).get("body", "")


def get_email_message(settings: dict, message_id: str) -> dict:
    """Fetch subject, from, and body for an ops mailbox message."""
    ops_mailbox = settings.get("ops_mailbox") or os.environ.get("OPS_MAILBOX")
    headers = _graph_headers(settings)

    url = (
        f"https://graph.microsoft.com/v1.0/users/{ops_mailbox}/messages/{message_id}"
        "?$select=body,subject,from"
    )
    resp = request_with_retry(requests.get, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    from_addr = (
        (data.get("from") or {}).get("emailAddress") or {}
    ).get("address", "")
    return {
        "body": data.get("body", {}).get("content", ""),
        "subject": data.get("subject", ""),
        "from_address": from_addr,
    }
