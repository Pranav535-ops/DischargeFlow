from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.audit.service import write_audit
from app.core.db import DB, utcnow
from app.core.security import AuthUser
from app.insurance.models import InsuranceClaim
from app.patients.models import Patient

router = APIRouter(prefix="/insurance", tags=["Insurance"])

ClaimStatus = Literal["pending", "submitted", "accepted", "denied", "paid"]


class InsuranceClaimCreate(BaseModel):
    patient_id: str
    insurer_name: str
    policy_number: str
    diagnosis_codes: list[str] = []
    procedure_codes: list[str] = []
    billed_amount: float


class ClaimStatusUpdate(BaseModel):
    status: ClaimStatus
    allowed_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    denial_reason: Optional[str] = None


@router.post("/claims")
async def create_claim(claim_data: InsuranceClaimCreate, request: Request, db: DB, user: AuthUser):
    if not await db.get(Patient, claim_data.patient_id):
        raise HTTPException(status_code=404, detail="Patient not found")
    claim = InsuranceClaim(**claim_data.model_dump(), created_by=user.user_id, submitted_at=utcnow())
    db.add(claim)
    await db.flush()
    await write_audit(db, user, "CREATE_CLAIM", "insurance_claim", claim.claim_id,
                      f"Billed: ${claim_data.billed_amount}", request)
    return {"claim_id": claim.claim_id, "status": "pending", "message": "Claim submitted successfully"}


@router.get("/claims")
async def get_claims(db: DB, user: AuthUser, patient_id: Optional[str] = None, status: Optional[str] = None):
    stmt = select(InsuranceClaim).order_by(InsuranceClaim.created_at.desc())
    if patient_id:
        stmt = stmt.where(InsuranceClaim.patient_id == patient_id)
    if status:
        stmt = stmt.where(InsuranceClaim.status == status)
    return [c.to_dict() for c in (await db.scalars(stmt))]


@router.get("/claims/{claim_id}")
async def get_claim(claim_id: str, db: DB, user: AuthUser):
    claim = await db.get(InsuranceClaim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    return claim.to_dict()


@router.put("/claims/{claim_id}/status")
async def update_claim_status(claim_id: str, update: ClaimStatusUpdate, request: Request, db: DB, user: AuthUser):
    claim = await db.get(InsuranceClaim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    claim.status = update.status
    if update.allowed_amount is not None:
        claim.allowed_amount = update.allowed_amount
    if update.paid_amount is not None:
        claim.paid_amount = update.paid_amount
    if update.denial_reason:
        claim.denial_reason = update.denial_reason
    await write_audit(db, user, "UPDATE_CLAIM_STATUS", "insurance_claim", claim_id, f"Status → {update.status}", request)
    return {"message": "Claim status updated"}


@router.get("/reports")
async def get_insurance_reports(db: DB, user: AuthUser):
    claims = list(await db.scalars(select(InsuranceClaim)))
    denied = sum(1 for c in claims if c.status == "denied")
    by_insurer: dict[str, dict] = {}
    for c in claims:
        row = by_insurer.setdefault(c.insurer_name or "Unknown", {"count": 0, "billed": 0.0, "paid": 0.0})
        row["count"] += 1
        row["billed"] += c.billed_amount or 0
        row["paid"] += c.paid_amount or 0
    return {
        "total_claims": len(claims),
        "total_billed": round(sum(c.billed_amount or 0 for c in claims), 2),
        "total_paid": round(sum(c.paid_amount or 0 for c in claims), 2),
        "total_allowed": round(sum(c.allowed_amount or 0 for c in claims), 2),
        "denial_rate": round(denied / len(claims) * 100, 1) if claims else 0,
        "pending_count": sum(1 for c in claims if c.status == "pending"),
        "accepted_count": sum(1 for c in claims if c.status in ("accepted", "paid")),
        "denied_count": denied,
        "by_insurer": [
            {"insurer": k, "count": v["count"], "billed": round(v["billed"], 2), "paid": round(v["paid"], 2)}
            for k, v in by_insurer.items()
        ],
    }
