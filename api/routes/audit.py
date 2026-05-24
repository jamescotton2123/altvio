"""
Audit ledger verification routes.
"""

from fastapi import APIRouter, Query

from core.audit import verify_audit_chain

router = APIRouter()


@router.get("/verify")
def verify_audit(firm_id: str = Query(...)):
    return verify_audit_chain(firm_id)
