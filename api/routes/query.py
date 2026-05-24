"""
Natural-language read-only data queries (ops / advisor).

POST /query — body { "question": "..." }, header X-Firm-ID
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core.nl_query import run_nl_query

router = APIRouter()


def _require_firm(firm_id: Optional[str]) -> str:
    if not firm_id:
        raise HTTPException(status_code=401, detail="X-Firm-ID header required.")
    return firm_id


class NlQueryPayload(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)


@router.post("")
def post_nl_query(
    payload: NlQueryPayload,
    x_firm_id: Optional[str] = Header(default=None),
):
    """
    Ask a question in plain English; returns safe RPC metadata, row results, and a one-line summary.
    Data is always scoped to the firm in X-Firm-ID.
    """
    firm_id = _require_firm(x_firm_id)
    try:
        return run_nl_query(payload.question, firm_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Query execution failed: {e}",
        ) from e
