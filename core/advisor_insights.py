"""
Advisor desk metrics for the Executive Command Center (CEO / managing partners).

Per-advisor numbers support rankings and drill-down; the firm-wide GPT brief is written
for leadership (capital at risk, which desks are stalling), not for ops coaching.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.commitment_status import SIGNED_STATES
from core.database import supabase
from core.http_retry import openai_chat_completion_with_retry
from core.openai_client import get_openai_client

_WIRE_OVERDUE_DAYS = 14
_LOOKBACK_DAYS = 90
_FIRM_EXECUTIVE_MARKER = "__firm_executive__"
logger = logging.getLogger(__name__)


def _parse_ts(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _days_between(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    return max(0, (end - start).days)


def _period_label(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _fmt_dollars(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


def _compute_metrics_for_advisor(
    advisor_email: str,
    investors: list[dict],
    commitments: list[dict],
    cutoff: datetime,
    kyc_followup_days: int,
    now: datetime,
) -> dict[str, Any]:
    advisor_investor_ids = {
        str(i["id"])
        for i in investors
        if _normalize_email(i.get("advisor_email")) == advisor_email
    }

    commitments_submitted = 0
    commitments_funded = 0
    docusign_day_samples: list[int] = []
    total_aum_committed = 0.0
    overdue_kyc_count = 0
    overdue_wire_count = 0

    for c in commitments:
        inv_id = str(c.get("investor_id") or "")
        if inv_id not in advisor_investor_ids:
            continue
        if (c.get("status") or "") != "Active":
            continue

        created = _parse_ts(c.get("created_at"))
        if created and created >= cutoff:
            commitments_submitted += 1

        if (c.get("wire_status") or "") == "Funded":
            commitments_funded += 1

        total_aum_committed += float(c.get("committed_amount") or 0)

        if (c.get("docusign_status") or "") in SIGNED_STATES:
            signed_at = _parse_ts(c.get("commitment_date")) or created
            if created and signed_at and signed_at >= cutoff:
                days = _days_between(created, signed_at)
                if days is not None:
                    docusign_day_samples.append(days)
            if (c.get("wire_status") or "") != "Funded" and created:
                if (now - created).days > _WIRE_OVERDUE_DAYS:
                    overdue_wire_count += 1

    kyc_day_samples: list[int] = []
    for inv in investors:
        if _normalize_email(inv.get("advisor_email")) != advisor_email:
            continue
        created = _parse_ts(inv.get("created_at"))
        if (inv.get("kyc_status") or "") == "Pending" and created:
            if (now - created).days > kyc_followup_days:
                overdue_kyc_count += 1
        if (inv.get("kyc_status") or "") == "Approved" and created and created >= cutoff:
            days = _days_between(created, now)
            if days is not None:
                kyc_day_samples.append(days)

    risk_score = overdue_kyc_count * 2 + overdue_wire_count * 3

    return {
        "advisor_email": advisor_email,
        "commitments_submitted": commitments_submitted,
        "commitments_funded": commitments_funded,
        "avg_days_kyc_pending": (
            round(sum(kyc_day_samples) / len(kyc_day_samples), 1) if kyc_day_samples else None
        ),
        "avg_days_docusign_pending": (
            round(sum(docusign_day_samples) / len(docusign_day_samples), 1)
            if docusign_day_samples
            else None
        ),
        "overdue_kyc_count": overdue_kyc_count,
        "overdue_wire_count": overdue_wire_count,
        "total_aum_committed": round(total_aum_committed, 2),
        "risk_score": risk_score,
        "lookback_days": _LOOKBACK_DAYS,
    }


def _firm_rollup(advisor_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "advisor_count": len(advisor_metrics),
        "total_aum_committed": round(
            sum(m.get("total_aum_committed") or 0 for m in advisor_metrics), 2
        ),
        "commitments_submitted_90d": sum(m.get("commitments_submitted") or 0 for m in advisor_metrics),
        "commitments_funded": sum(m.get("commitments_funded") or 0 for m in advisor_metrics),
        "total_overdue_kyc": sum(m.get("overdue_kyc_count") or 0 for m in advisor_metrics),
        "total_overdue_wires": sum(m.get("overdue_wire_count") or 0 for m in advisor_metrics),
        "desks_with_pipeline_risk": sum(
            1 for m in advisor_metrics if (m.get("overdue_kyc_count") or 0) + (m.get("overdue_wire_count") or 0) > 0
        ),
    }


def _gpt_executive_brief(firm_metrics: dict[str, Any], advisor_metrics: list[dict[str, Any]]) -> str:
    by_risk = sorted(advisor_metrics, key=lambda m: m.get("risk_score", 0), reverse=True)
    attention = [
        {
            "advisor_email": m["advisor_email"],
            "total_aum_committed": m["total_aum_committed"],
            "overdue_kyc_count": m["overdue_kyc_count"],
            "overdue_wire_count": m["overdue_wire_count"],
        }
        for m in by_risk[:8]
        if m.get("risk_score", 0) > 0
    ]
    response = openai_chat_completion_with_retry(
        get_openai_client().chat.completions.create,
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": (
                    "You are briefing the CEO / managing partners of an alternative investment firm. "
                    "Write a concise executive paragraph (3–4 sentences) on advisor desk health: "
                    "where capital is committed, which advisor books have onboarding friction, "
                    "and what leadership should ask about. Avoid ops process jargon. "
                    f"Firm roll-up:\n{json.dumps(firm_metrics, default=str)}\n\n"
                    f"Desks with the most friction (if any):\n{json.dumps(attention, default=str)}"
                ),
            }
        ],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _load_period_rows(firm_id: str, period_label: str | None = None) -> list[dict]:
    q = (
        supabase.table("advisor_insight_reports")
        .select("*")
        .eq("firm_id", firm_id)
        .order("generated_at", desc=True)
    )
    if period_label:
        q = q.eq("period_label", period_label)
    return q.limit(200).execute().data or []


def _latest_period_label(rows: list[dict]) -> str | None:
    for row in rows:
        pl = row.get("period_label")
        if pl:
            return pl
    return None


def build_executive_insights_view(firm_id: str) -> dict[str, Any]:
    """
    Firm-wide executive view: roll-up totals, rankings, GPT brief, per-desk metrics.
    """
    all_rows = _load_period_rows(firm_id)
    period = _latest_period_label(all_rows)
    if not period:
        return {
            "period_label": None,
            "generated_at": None,
            "executive_summary": (
                "No advisor desk report has been generated yet. "
                "Run POST /exec/advisor-insights/generate or wait for the weekly Monday job."
            ),
            "firm_rollup": {},
            "desks_needing_attention": [],
            "top_desks_by_committed": [],
            "advisor_desks": [],
        }

    period_rows = [r for r in all_rows if r.get("period_label") == period]
    firm_row = next(
        (r for r in period_rows if (r.get("report_data") or {}).get("report_type") == "firm_executive"),
        None,
    )
    advisor_rows = [
        r for r in period_rows
        if (r.get("report_data") or {}).get("advisor_email")
        and (r.get("report_data") or {}).get("report_type") != "firm_executive"
    ]

    desks = []
    for row in advisor_rows:
        data = row.get("report_data") or {}
        metrics = data.get("metrics") or {}
        desks.append({
            "advisor_email": data.get("advisor_email"),
            "metrics": metrics,
            "card_line": row.get("summary_text"),
        })

    by_committed = sorted(
        desks,
        key=lambda d: (d.get("metrics") or {}).get("total_aum_committed") or 0,
        reverse=True,
    )
    by_risk = sorted(
        desks,
        key=lambda d: (d.get("metrics") or {}).get("risk_score") or 0,
        reverse=True,
    )

    return {
        "period_label": period,
        "generated_at": (firm_row or period_rows[0]).get("generated_at") if period_rows else None,
        "executive_summary": (firm_row or {}).get("summary_text") or "",
        "firm_rollup": (firm_row or {}).get("report_data", {}).get("firm_rollup") or _firm_rollup(
            [d["metrics"] for d in desks]
        ),
        "desks_needing_attention": [
            {
                "advisor_email": d["advisor_email"],
                "total_aum_committed": d["metrics"].get("total_aum_committed"),
                "overdue_kyc_count": d["metrics"].get("overdue_kyc_count"),
                "overdue_wire_count": d["metrics"].get("overdue_wire_count"),
                "risk_score": d["metrics"].get("risk_score"),
            }
            for d in by_risk
            if (d["metrics"].get("risk_score") or 0) > 0
        ][:10],
        "top_desks_by_committed": [
            {
                "advisor_email": d["advisor_email"],
                "total_aum_committed": d["metrics"].get("total_aum_committed"),
                "commitments_funded": d["metrics"].get("commitments_funded"),
            }
            for d in by_committed[:10]
        ],
        "advisor_desks": desks,
    }


def generate_advisor_insights(firm_id: str) -> dict[str, Any]:
    """
    Generates per-advisor metric rows plus one firm_executive row with leadership brief.
    Returns { period_label, advisor_reports, executive_report }.
    """
    settings = (
        supabase.table("firm_settings")
        .select("kyc_followup_days")
        .eq("firm_id", firm_id)
        .single()
        .execute()
        .data
    ) or {}
    kyc_followup_days = int(settings.get("kyc_followup_days") or 5)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_LOOKBACK_DAYS)
    period = _period_label(now)

    investors = (
        supabase.table("investors")
        .select("id, advisor_email, kyc_status, created_at")
        .eq("firm_id", firm_id)
        .execute()
        .data
        or []
    )

    commitments = (
        supabase.table("commitments")
        .select(
            "id, investor_id, committed_amount, wire_status, docusign_status, "
            "status, created_at, commitment_date"
        )
        .eq("firm_id", firm_id)
        .execute()
        .data
        or []
    )

    active_inv_ids = {
        str(c["investor_id"])
        for c in commitments
        if c.get("status") == "Active" and c.get("investor_id")
    }
    advisor_emails: set[str] = set()
    for inv in investors:
        if str(inv["id"]) not in active_inv_ids:
            continue
        em = _normalize_email(inv.get("advisor_email"))
        if em:
            advisor_emails.add(em)

    advisor_metrics_list: list[dict[str, Any]] = []
    advisor_reports: list[dict] = []

    for advisor_email in sorted(advisor_emails):
        metrics = _compute_metrics_for_advisor(
            advisor_email, investors, commitments, cutoff, kyc_followup_days, now,
        )
        advisor_metrics_list.append(metrics)
        card = (
            f"{advisor_email}: {_fmt_dollars(metrics['total_aum_committed'])} committed, "
            f"{metrics['commitments_funded']} funded"
        )
        if metrics["overdue_kyc_count"] or metrics["overdue_wire_count"]:
            card += (
                f" — {metrics['overdue_kyc_count']} KYC / {metrics['overdue_wire_count']} wire overdue"
            )
        row = {
            "firm_id": firm_id,
            "period_label": period,
            "report_data": {
                "report_type": "advisor_desk",
                "advisor_email": advisor_email,
                "metrics": metrics,
            },
            "summary_text": card,
        }
        ins = supabase.table("advisor_insight_reports").insert(row).execute()
        if ins.data:
            advisor_reports.append(ins.data[0])

    firm_rollup = _firm_rollup(advisor_metrics_list)
    executive_summary = _gpt_executive_brief(firm_rollup, advisor_metrics_list)

    exec_row = {
        "firm_id": firm_id,
        "period_label": period,
        "report_data": {
            "report_type": "firm_executive",
            "advisor_email": _FIRM_EXECUTIVE_MARKER,
            "firm_rollup": firm_rollup,
        },
        "summary_text": executive_summary,
    }
    exec_ins = supabase.table("advisor_insight_reports").insert(exec_row).execute()
    executive_report = exec_ins.data[0] if exec_ins.data else None

    logger.info("Advisor executive desk report %s generated for %s advisor(s).", period, len(advisor_reports))

    return {
        "period_label": period,
        "advisor_reports": advisor_reports,
        "executive_report": executive_report,
    }


def get_latest_advisor_insight(firm_id: str, advisor_email: str) -> dict | None:
    """Latest per-desk row for drill-down (Executive Command Center)."""
    target = _normalize_email(advisor_email)
    rows = _load_period_rows(firm_id)
    for row in rows:
        data = row.get("report_data") or {}
        if data.get("report_type") == "firm_executive":
            continue
        if _normalize_email(data.get("advisor_email")) == target:
            return row
    return None
