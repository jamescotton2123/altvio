"""
Firm-scoped audit and AI invocation reporting endpoints.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from core.database import supabase

router = APIRouter()

AUDIT_PUBLIC_COLUMNS = [
    "id",
    "actor_type",
    "actor_id",
    "action",
    "before",
    "after",
    "metadata",
    "created_at",
]


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


def _default_since() -> date:
    return date.today() - timedelta(days=30)


def _decimal_to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


@router.get("/audit/firm/summary")
def firm_audit_summary(
    since: Optional[date] = Query(default=None),
    x_firm_id: Optional[str] = Header(default=None),
):
    firm_id = _require_firm(x_firm_id)
    since = since or _default_since()
    rows = (
        supabase.table("audit_logs")
        .select("action")
        .eq("firm_id", firm_id)
        .gte("created_at", since.isoformat())
        .execute()
        .data
        or []
    )

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.get("action")] += 1

    summary = [
        {"action": action, "count": count}
        for action, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
        if action
    ]
    return {"since": since.isoformat(), "summary": summary}


@router.get("/ai/invocations/cost-summary")
def ai_invocation_cost_summary(
    since: Optional[date] = Query(default=None),
    x_firm_id: Optional[str] = Header(default=None),
):
    firm_id = _require_firm(x_firm_id)
    since = since or _default_since()
    rows = (
        supabase.table("ai_invocations")
        .select("engine, task, input_tokens, output_tokens, cost_usd, latency_ms")
        .eq("firm_id", firm_id)
        .gte("created_at", since.isoformat())
        .execute()
        .data
        or []
    )

    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.get("engine"), row.get("task"))
        if not all(key):
            continue

        bucket = grouped.setdefault(
            key,
            {
                "engine": key[0],
                "task": key[1],
                "call_count": 0,
                "total_cost_usd": 0.0,
                "total_tokens": 0,
                "latency_total_ms": 0,
                "latency_count": 0,
            },
        )
        bucket["call_count"] += 1
        bucket["total_cost_usd"] += _decimal_to_float(row.get("cost_usd"))
        bucket["total_tokens"] += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)

        latency_ms = row.get("latency_ms")
        if latency_ms is not None:
            bucket["latency_total_ms"] += int(latency_ms)
            bucket["latency_count"] += 1

    by_engine = []
    for bucket in grouped.values():
        latency_count = bucket.pop("latency_count")
        latency_total_ms = bucket.pop("latency_total_ms")
        bucket["total_cost_usd"] = round(bucket["total_cost_usd"], 6)
        bucket["avg_latency_ms"] = round(latency_total_ms / latency_count, 2) if latency_count else None
        by_engine.append(bucket)

    by_engine.sort(key=lambda row: row["total_cost_usd"], reverse=True)
    return {"since": since.isoformat(), "by_engine": by_engine}


@router.get("/audit/{entity_type}/{entity_id}")
def entity_audit_log(
    entity_type: str,
    entity_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    x_firm_id: Optional[str] = Header(default=None),
):
    firm_id = _require_firm(x_firm_id)
    result = (
        supabase.table("audit_logs")
        .select(", ".join(AUDIT_PUBLIC_COLUMNS), count="exact")
        .eq("firm_id", firm_id)
        .eq("entity_type", entity_type)
        .eq("entity_id", entity_id)
        .order("id", desc=False)
        .range(offset, offset + limit - 1)
        .execute()
    )

    rows = [
        {column: row.get(column) for column in AUDIT_PUBLIC_COLUMNS}
        for row in (result.data or [])
    ]
    count = getattr(result, "count", None)
    return {"rows": rows, "total": count if count is not None else len(rows)}
