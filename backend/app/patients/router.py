from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.models import DischargeSummary
from app.audit.service import audit_phi_read, write_audit
from app.core.db import DB
from app.core.security import AuthUser, CurrentUser, requires
from app.patients.models import Patient

router = APIRouter(prefix="/patients", tags=["Patients"])


class PatientCreate(BaseModel):
    name: str
    date_of_birth: str
    admission_date: str
    room_number: str
    diagnosis: str
    attending_physician: str


class PatientUpdate(BaseModel):
    """Editable fields. Unknown keys are ignored, so clients can't overwrite IDs, timestamps or erasure flags."""

    name: Optional[str] = None
    date_of_birth: Optional[str] = None
    admission_date: Optional[str] = None
    room_number: Optional[str] = None
    diagnosis: Optional[str] = None
    attending_physician: Optional[str] = None
    status: Optional[Literal["admitted", "pending_discharge", "discharged"]] = None


class ConsentUpdate(BaseModel):
    gdpr_consent: bool


async def get_patient_or_404(db, patient_id: str) -> Patient:
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


@router.get("")
async def get_patients(request: Request, db: DB, user: AuthUser, status: Optional[str] = None):
    stmt = select(Patient).order_by(Patient.admission_date.desc())
    if status:
        stmt = stmt.where(Patient.status == status)
    patients = [p.to_dict() for p in (await db.scalars(stmt))]
    # One entry for the whole list, not one per row.
    await audit_phi_read(db, user, None, f"patient list ({len(patients)} records)", request)
    return patients


@router.get("/{patient_id}")
async def get_patient(patient_id: str, request: Request, db: DB, user: AuthUser):
    patient = await get_patient_or_404(db, patient_id)
    await audit_phi_read(db, user, patient_id, "patient record", request)
    return patient.to_dict()


@router.post("")
async def create_patient(
    patient_data: PatientCreate, request: Request, db: DB, user: CurrentUser = Depends(requires("patient:write"))
):
    patient = Patient(**patient_data.model_dump())
    db.add(patient)
    await db.flush()
    await write_audit(db, user, "CREATE_PATIENT", "patient", patient.patient_id, "Patient record created", request)
    return patient.to_dict()


@router.put("/{patient_id}")
async def update_patient(
    patient_id: str,
    updates: PatientUpdate,
    request: Request,
    db: DB,
    user: CurrentUser = Depends(requires("patient:write")),
):
    patient = await get_patient_or_404(db, patient_id)
    changes = updates.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(patient, field, value)
    await db.flush()
    # Field names only: the values are PHI, and audit rows can't be erased.
    await write_audit(db, user, "UPDATE_PATIENT", "patient", patient_id,
                      f"fields: {', '.join(sorted(changes)) or 'none'}", request)
    await db.refresh(patient)
    return patient.to_dict()


@router.post("/{patient_id}/consent")
async def update_patient_consent(patient_id: str, body: ConsentUpdate, request: Request, db: DB, user: AuthUser):
    patient = await get_patient_or_404(db, patient_id)
    patient.gdpr_consent = body.gdpr_consent
    await write_audit(db, user, "UPDATE_CONSENT", "patient", patient_id, f"GDPR consent → {body.gdpr_consent}", request)
    return {"message": "Consent updated", "gdpr_consent": body.gdpr_consent}


@router.delete("/{patient_id}/data")
async def delete_patient_data(
    patient_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("patient:erase"))
):
    """GDPR right-to-erasure: destroy the patient's data key and overwrite identifiers and discharge summaries.

    The row stays so audit references remain valid.
    """
    patient = await get_patient_or_404(db, patient_id)
    patient.shred()
    for summary in await db.scalars(select(DischargeSummary).where(DischargeSummary.patient_id == patient_id)):
        summary.shred()
    await write_audit(db, user, "GDPR_ERASURE", "patient", patient_id, "Patient data key destroyed and identifiers erased", request)
    return {"message": "Patient data erased per GDPR right-to-be-forgotten"}
