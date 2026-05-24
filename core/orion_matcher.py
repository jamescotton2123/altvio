"""
Orion household fuzzy matching.
Uses rapidfuzz to compare investor entity names against known Orion households.
Prevents duplicate householding when existing investors onboard into new deals.

Matching tiers:
  ≥ 95 — auto-confirmed, proceeds directly to NAImport (Existing)
  75–94 — flagged for ops review in the Deal Hub review queue
  < 75  — flagged as 'No Match Found' for manual resolution

The NAImport export script will refuse to run for a deal that has
any investor still in 'Needs Review' status.
"""

import csv
import logging
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from core.database import supabase

HOUSEHOLDS_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "orion_households.csv"
AUTO_CONFIRM_THRESHOLD = 95
REVIEW_THRESHOLD = 75
logger = logging.getLogger(__name__)


def load_household_list() -> list[str]:
    """
    Load the known Orion household name list from the reference CSV.
    The CSV must have a column named 'household_name'.
    Upload this file once from an Orion export.
    """
    if not HOUSEHOLDS_CSV_PATH.exists():
        logger.warning("Household reference file not found at %s", HOUSEHOLDS_CSV_PATH)
        return []

    names = []
    with open(HOUSEHOLDS_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("household_name", "").strip()
            if name:
                names.append(name)
    return names


def match_investor(
    investor_id: str,
    firm_id: str,
    entity_name: str,
    households: Optional[list[str]] = None,
) -> dict:
    """
    Run fuzzy match for a single investor against the Orion household list.
    Writes results to orion_match_candidates and updates investor.orion_match_status.

    Returns:
    {
        "status": "auto_confirmed" | "needs_review" | "no_match",
        "matched_name": str | None,
        "score": float | None,
        "candidates": list[dict]
    }
    """
    if households is None:
        households = load_household_list()

    if not households:
        return {"status": "no_match", "matched_name": None, "score": None, "candidates": []}

    # Get top 5 candidate matches
    results = process.extract(
        entity_name,
        households,
        scorer=fuzz.WRatio,
        limit=5,
    )

    candidates = [{"name": name, "score": round(score, 1)} for name, score, _ in results]
    top_match, top_score = results[0][0], results[0][1] if results else (None, 0)

    if top_score >= AUTO_CONFIRM_THRESHOLD:
        # Auto-confirm — update investor directly
        supabase.table("investors").update({
            "orion_household_name": top_match,
            "orion_match_status": "Confirmed",
        }).eq("id", investor_id).execute()

        return {
            "status": "auto_confirmed",
            "matched_name": top_match,
            "score": top_score,
            "candidates": candidates,
        }

    elif top_score >= REVIEW_THRESHOLD:
        status = "Needs Review"
    else:
        status = "No Match Found"

    # Write to review queue
    supabase.table("orion_match_candidates").upsert({
        "firm_id": firm_id,
        "investor_id": investor_id,
        "candidates": candidates,
        "status": "Pending",
    }, on_conflict="investor_id").execute()

    supabase.table("investors").update({
        "orion_match_status": status,
    }).eq("id", investor_id).execute()

    return {
        "status": "needs_review" if status == "Needs Review" else "no_match",
        "matched_name": top_match,
        "score": top_score,
        "candidates": candidates,
    }


def confirm_match(investor_id: str, confirmed_name: str, reviewed_by: Optional[str] = None) -> dict:
    """
    Ops confirms a specific household match from the review queue.
    Locks in orion_household_name and sets orion_match_status = 'Confirmed'.
    """
    from datetime import datetime, timezone

    supabase.table("investors").update({
        "orion_household_name": confirmed_name,
        "orion_match_status": "Confirmed",
    }).eq("id", investor_id).execute()

    supabase.table("orion_match_candidates").update({
        "confirmed_household_name": confirmed_name,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "status": "Confirmed",
    }).eq("investor_id", investor_id).execute()

    return {"status": "confirmed", "investor_id": investor_id, "household_name": confirmed_name}


def run_deal_matching(deal_id: str, firm_id: str) -> dict:
    """
    Run fuzzy matching for all existing investors (orion_id IS NOT NULL)
    being onboarded into a specific deal. Call before generating NAImports.
    """
    households = load_household_list()

    commitments = (
        supabase.table("commitments")
        .select("investor_id, investors(id, entity_name, orion_id, orion_match_status)")
        .eq("deal_id", deal_id)
        .eq("firm_id", firm_id)
        .eq("status", "Active")
        .execute()
        .data
    )

    results = {"auto_confirmed": [], "needs_review": [], "no_match": [], "new_investors": []}

    for c in commitments:
        investor = c.get("investors", {})
        if not investor.get("orion_id"):
            results["new_investors"].append(investor.get("entity_name"))
            continue

        if investor.get("orion_match_status") == "Confirmed":
            results["auto_confirmed"].append(investor.get("entity_name"))
            continue

        match = match_investor(
            investor_id=investor["id"],
            firm_id=firm_id,
            entity_name=investor["entity_name"],
            households=households,
        )
        results[match["status"].replace("auto_", "")].append({
            "entity_name": investor["entity_name"],
            "match": match,
        })

    return results
