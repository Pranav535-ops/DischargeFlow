from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.insurance.models import InsuranceClaim
from app.patients.models import Patient


async def create_claim(db: AsyncSession, user: CurrentUser, *, patient_id: str, insurer_name: str,
                       policy_number: str, billed_amount: float, diagnosis_codes: list[str] | None = None,
                       procedure_codes: list[str] | None = None, request: Optional[Request] = None) -> InsuranceClaim:
    if not await db.get(Patient, patient_id):
        raise HTTPException(status_code=404, detail="Patient not found")
    claim = InsuranceClaim(patient_id=patient_id, insurer_name=insurer_name, policy_number=policy_number,
                           billed_amount=billed_amount, diagnosis_codes=diagnosis_codes or [],
                           procedure_codes=procedure_codes or [], created_by=user.user_id, submitted_at=utcnow())
    db.add(claim)
    await db.flush()
    await write_audit(db, user, "CREATE_CLAIM", "insurance_claim", claim.claim_id, f"Billed: ${billed_amount}", request)
    return claim
