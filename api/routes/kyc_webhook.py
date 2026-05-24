"""
SharePoint / KYC webhook routes.
POST /kyc/webhook — fires when a file is uploaded to an investor's KYC folder.
                    Supports .zip uploads — extracts each file and processes individually.
POST /kyc/ask     — advisor natural language completeness Q&A.
Triggers the agentic KYC parser (GPT-4o Vision).
"""

import io
import logging
import secrets
import zipfile
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from core.database import supabase
from core.kyc_templates import get_checklist

router = APIRouter()
logger = logging.getLogger(__name__)

INVESTOR_REVIEW_STATUSES = (
    "Reviewing",
    "Escalated",
    "Needs More",
    "Pending Review",
    "Pending",
)
REVIEW_ROW_OPEN_STATUSES = ("Pending", "Reviewing", "Needs More", "Escalated")

# Parser fields (see core/kyc_parser.py). Omit document_type_detected / confidence — not DB columns.
_KYC_REVIEW_QUEUE_SELECT = (
    "id, investor_id, status, matched_docs, nested_entities, signatories, "
    "flags, formation_date, ownership_structure, escalated_to_compliance"
)
_KYC_REVIEW_QUEUE_SELECT_MINIMAL = (
    "id, investor_id, status, ownership_structure, escalated_to_compliance"
)


def _fetch_kyc_reviews_for_queue(firm_id: str) -> list[dict]:
    for columns in (_KYC_REVIEW_QUEUE_SELECT, _KYC_REVIEW_QUEUE_SELECT_MINIMAL):
        try:
            rows = (
                supabase.table("kyc_reviews")
                .select(columns)
                .eq("firm_id", firm_id)
                .limit(500)
                .execute()
                .data
                or []
            )
            rows.sort(key=lambda r: str(r.get("id") or ""), reverse=True)
            return rows
        except Exception as exc:
            err = getattr(exc, "args", (None,))[0]
            code = err.get("code") if isinstance(err, dict) else None
            if columns == _KYC_REVIEW_QUEUE_SELECT_MINIMAL:
                raise
            if code not in ("42703", "PGRST204"):
                raise
            logger.warning(
                "kyc_reviews queue: falling back to minimal columns (%s)", exc
            )
    return []


def _require_firm(x_firm_id: Optional[str]) -> str:
    if not x_firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return x_firm_id


def _detected_doc_type(review: dict) -> Optional[str]:
    if review.get("document_type_detected"):
        return str(review["document_type_detected"])
    docs = review.get("matched_docs") or []
    if docs:
        return str(docs[0])
    return None


def _infer_confidence(review: dict) -> str:
    raw = str(review.get("confidence") or "").strip().lower()
    if raw in ("high", "medium", "low"):
        return raw
    if review.get("escalated_to_compliance"):
        return "low"
    return "medium"


def _requested_doc_type(entity_type: str) -> Optional[str]:
    checklist = get_checklist(entity_type or "Individual")
    return checklist[0] if checklist else None


def _merge_queue_item(investor: dict, review: Optional[dict]) -> Optional[dict]:
    kyc_status = str(investor.get("kyc_status") or "Reviewing")
    in_review = kyc_status in INVESTOR_REVIEW_STATUSES
    review_open = review and str(review.get("status") or "") in REVIEW_ROW_OPEN_STATUSES
    if not in_review and not review_open:
        return None

    entity_type = str(investor.get("entity_type") or "Individual")
    detected = _detected_doc_type(review) if review else None
    requested = _requested_doc_type(entity_type)

    matches = review.get("matches_requested_type") if review else None
    if matches is None and detected and requested:
        d = detected.lower()
        r = requested.lower()
        matches = (
            r.split("/")[0].strip() in d
            or d.split(" ")[0] in r
        )

    formation = None
    if review and review.get("formation_date"):
        formation = str(review["formation_date"])
    elif investor.get("formation_date"):
        formation = str(investor["formation_date"])

    return {
        "investor_id": investor["id"],
        "review_id": review.get("id") if review else None,
        "entity_name": investor.get("entity_name") or "Unknown entity",
        "entity_type": entity_type,
        "kyc_status": kyc_status,
        "document_type_detected": detected,
        "requested_doc_type": requested,
        "matches_requested_type": matches,
        "confidence": _infer_confidence(review) if review else "medium",
        "formation_date": formation,
        "state_of_formation": investor.get("state_of_formation"),
        "ownership_structure": (review or {}).get("ownership_structure"),
        "nested_entities": (review or {}).get("nested_entities") or [],
        "signatories": (review or {}).get("signatories") or [],
        "flags": (review or {}).get("flags") or [],
        "submitted_at": (review or {}).get("created_at"),
    }


@router.get("/review-queue")
def get_kyc_review_queue(x_firm_id: Optional[str] = Header(default=None)):
    """
    Ops KYC review queue: investors awaiting sign-off with latest AI parser output
    from kyc_reviews (nested entities, signatories, flags, ownership structure).
    """
    firm_id = _require_firm(x_firm_id)

    investors = (
        supabase.table("investors")
        .select(
            "id, entity_name, entity_type, kyc_status, formation_date, state_of_formation"
        )
        .eq("firm_id", firm_id)
        .in_("kyc_status", list(INVESTOR_REVIEW_STATUSES))
        .execute()
        .data
        or []
    )
    investor_map = {inv["id"]: inv for inv in investors}

    try:
        reviews = _fetch_kyc_reviews_for_queue(firm_id)
    except Exception as exc:
        logger.exception("kyc review queue: kyc_reviews query failed")
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not load kyc_reviews. Apply migration 20260538_kyc_reviews_api.sql "
                "and use the Supabase service_role key in local .env (SUPABASE_KEY). "
                f"({exc})"
            ),
        ) from exc

    latest_by_investor: dict[str, dict] = {}
    for row in reviews:
        inv_id = row["investor_id"]
        if inv_id not in latest_by_investor:
            latest_by_investor[inv_id] = row

    queue: dict[str, dict] = {}
    for inv in investor_map.values():
        item = _merge_queue_item(inv, latest_by_investor.get(inv["id"]))
        if item:
            queue[inv["id"]] = item

    for inv_id, review in latest_by_investor.items():
        if inv_id in queue:
            continue
        inv = investor_map.get(inv_id) or {
            "id": inv_id,
            "entity_name": "Unknown entity",
            "kyc_status": "Reviewing",
            "entity_type": "Individual",
        }
        item = _merge_queue_item(inv, review)
        if item:
            queue[inv_id] = item

    results = list(queue.values())
    results.sort(
        key=lambda x: str(x.get("submitted_at") or ""),
        reverse=True,
    )
    return results


def _get_investor_by_folder(folder_id: str) -> dict:
    result = (
        supabase.table("investors")
        .select("id, firm_id, entity_name")
        .eq("sharepoint_folder_id", folder_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"No investor found for folder_id={folder_id}")
    return result.data


def _get_firm_settings(firm_id: str) -> dict:
    result = (
        supabase.table("firm_settings").select("*").eq("firm_id", firm_id).single().execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Firm settings not found.")
    return result.data


def _graph_client_state_is_valid(notification: dict, settings: dict) -> bool:
    expected = settings.get("graph_subscription_client_state")
    actual = notification.get("clientState")
    if expected and actual and secrets.compare_digest(str(actual), str(expected)):
        return True

    logger.warning(
        "[KYCWebhook] Skipping notification with invalid Microsoft Graph clientState for firm_id=%s",
        settings.get("firm_id"),
    )
    return False


@router.post("/webhook")
async def kyc_webhook(request: Request):
    """
    Receive Microsoft Graph change notification when a file is created
    in a SharePoint KYC folder.
    """
    # Handle Graph subscription validation handshake
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=validation_token)

    body = await request.json()
    notifications = body.get("value", [])

    results = []
    for notification in notifications:
        resource = notification.get("resource", "")
        drive_item_id = notification.get("resourceData", {}).get("id")

        if not drive_item_id:
            continue

        # Extract the parent folder ID from the resource path
        # Resource format: drives/{driveId}/items/{itemId}
        parts = resource.split("/")
        if len(parts) < 4:
            continue

        drive_id = parts[1] if len(parts) > 1 else None

        try:
            # We need the parent folder ID — fetch the item details from Graph
            folder_id = notification.get("resourceData", {}).get("parentReference", {}).get("id")
            if not folder_id:
                continue

            investor = _get_investor_by_folder(folder_id)
            investor_id = investor["id"]
            firm_id = investor["firm_id"]
            settings = _get_firm_settings(firm_id)
            if not _graph_client_state_is_valid(notification, settings):
                continue
            insert_result = supabase.table("webhook_events").upsert({
                "source": "graph",
                "external_id": drive_item_id,
                "firm_id": firm_id,
                "payload": notification,
            }, on_conflict="source,external_id", ignore_duplicates=True).execute()
            if not insert_result.data:
                results.append({"status": "already_processed", "drive_item_id": drive_item_id})
                continue

            # Download the file bytes from SharePoint
            import requests as req

            from core.graph_client import _get_access_token
            from core.http_retry import REQUEST_TIMEOUT_SECONDS, request_with_retry

            site_id = settings.get("sharepoint_site_id")
            token = _get_access_token(settings)
            file_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/items/{drive_item_id}/content"
            file_resp = request_with_retry(
                req.get,
                file_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            file_resp.raise_for_status()
            file_bytes = file_resp.content

            filename = notification.get("resourceData", {}).get("name", "document.pdf")

            # Build source doc URL for ops pre-fill review context
            site_id = settings.get("sharepoint_site_id", "")
            source_doc_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/items/{drive_item_id}"
                if site_id and drive_id and drive_item_id else None
            )

            from core.kyc_parser import process_kyc_upload

            # Handle zip uploads — extract and process each file individually
            if filename.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                        for member_name in zf.namelist():
                            # Skip hidden files and directories
                            if member_name.startswith("__") or member_name.endswith("/"):
                                continue
                            member_bytes = zf.read(member_name)
                            result = process_kyc_upload(
                                investor_id=investor_id,
                                firm_id=firm_id,
                                filename=member_name,
                                file_bytes=member_bytes,
                                settings=settings,
                                source_archive=filename,
                                source_doc_url=source_doc_url,
                            )
                            results.append(result)
                except zipfile.BadZipFile:
                    logger.warning("KYC webhook could not extract zip file: %s", filename)
                    continue
            else:
                result = process_kyc_upload(
                    investor_id=investor_id,
                    firm_id=firm_id,
                    filename=filename,
                    file_bytes=file_bytes,
                    settings=settings,
                    source_doc_url=source_doc_url,
                )
                results.append(result)

        except Exception as e:
            logger.error("KYC webhook error processing notification: %s", e)
            continue

    return {"processed": len(results), "results": results}


class KycAskPayload(BaseModel):
    investor_id: str
    question: str


@router.post("/ask")
def kyc_ask(payload: KycAskPayload, x_firm_id: Optional[str] = Header(default=None)):
    """
    Advisor Q&A interface: answer natural language questions about an investor's KYC status.
    Example: "Did ABC LLC upload everything?" or "What documents are we still waiting on?"
    """
    firm_id = x_firm_id
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")

    investor = (
        supabase.table("investors")
        .select("entity_name, entity_type, kyc_status")
        .eq("id", payload.investor_id)
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    )
    if not investor:
        raise HTTPException(status_code=404, detail="Investor not found.")

    reviews = (
        supabase.table("kyc_reviews")
        .select("matched_docs, flags, status, escalated_to_compliance")
        .eq("investor_id", payload.investor_id)
        .eq("firm_id", firm_id)
        .execute()
        .data
    )

    from core.kyc_templates import get_checklist
    checklist = get_checklist(investor.get("entity_type", "Individual"))

    received_docs = []
    all_flags = []
    for r in reviews:
        received_docs.extend(r.get("matched_docs") or [])
        all_flags.extend(r.get("flags") or [])
    received_docs = list(set(received_docs))

    # Determine missing docs
    missing = []
    for item in checklist:
        item_key = item.lower().split("(")[0].strip()
        if not any(item_key in doc.lower() or doc.lower() in item.lower() for doc in received_docs):
            missing.append(item)

    context = (
        f"Investor: {investor['entity_name']} ({investor.get('entity_type', 'Unknown')})\n"
        f"KYC Status: {investor.get('kyc_status', 'Unknown')}\n"
        f"Required Documents: {', '.join(checklist)}\n"
        f"Documents Received: {', '.join(received_docs) if received_docs else 'None yet'}\n"
        f"Missing Documents: {', '.join(missing) if missing else 'None — all received'}\n"
        f"Flags/Issues: {', '.join(all_flags) if all_flags else 'None'}\n"
        f"Escalated to Compliance: {any(r.get('escalated_to_compliance') for r in reviews)}\n"
    )

    import os

    from openai import OpenAI

    from core.http_retry import (
        AI_CLIENT_TIMEOUT_SECONDS,
        openai_chat_completion_with_retry,
    )
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=AI_CLIENT_TIMEOUT_SECONDS)
    response = openai_chat_completion_with_retry(
        client.chat.completions.create,
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a KYC operations assistant. Answer the advisor's question about this investor's "
                    "KYC status concisely and in plain English. Be specific about what's missing if applicable."
                ),
            },
            {"role": "user", "content": f"Investor KYC context:\n{context}\n\nAdvisor question: {payload.question}"},
        ],
        temperature=0,
    )

    return {
        "investor_id": payload.investor_id,
        "entity_name": investor["entity_name"],
        "question": payload.question,
        "answer": response.choices[0].message.content,
        "kyc_summary": {
            "status": investor.get("kyc_status"),
            "documents_received": received_docs,
            "missing_documents": missing,
            "flags": all_flags,
        },
    }
