from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai.models import DischargeSummary, PatientInstructions
from app.audit.service import audit_phi_read, write_audit
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, requires
from app.discharge.planning import redact_delay_notes
from app.identity.models import User
from app.patients import service
from app.patients.models import LANGUAGE_TAG_PATTERN, AccessGrant, Encounter, Patient

router = APIRouter(prefix="/patients", tags=["Patients"])
access_router = APIRouter(prefix="/access-grants", tags=["Patients"])


class PatientCreate(BaseModel):
    name: str
    date_of_birth: str
    admission_date: str
    room_number: str
    diagnosis: str
    attending_physician: str
    unit_id: Optional[str] = None
    preferred_language: Optional[str] = Field(default=None, pattern=LANGUAGE_TAG_PATTERN)


class PatientUpdate(BaseModel):
    """Editable fields. Unknown keys are ignored, so clients can't overwrite IDs, timestamps or erasure flags."""

    name: Optional[str] = None
    date_of_birth: Optional[str] = None
    admission_date: Optional[str] = None
    room_number: Optional[str] = None
    diagnosis: Optional[str] = None
    attending_physician: Optional[str] = None
    unit_id: Optional[str] = None
    preferred_language: Optional[str] = Field(default=None, pattern=LANGUAGE_TAG_PATTERN)
    status: Optional[Literal["admitted", "pending_discharge", "discharged"]] = None


class ConsentUpdate(BaseModel):
    gdpr_consent: bool


class BreakGlassRequest(BaseModel):
    # Long enough to be a real reason; reviewed by admins at GET /api/access-grants.
    reason: str = Field(min_length=10, max_length=500)


async def get_patient_or_404(db, patient_id: str) -> Patient:
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


@router.get("")
async def get_patients(request: Request, db: DB, user: AuthUser, status: Optional[str] = None):
    # Records merged into another by an HL7 A40 stay for audit references but are not listed.
    stmt = select(Patient).where(Patient.merged_into.is_(None)).order_by(Patient.admission_date.desc())
    if status:
        stmt = stmt.where(Patient.status == status)
    patients = [p.to_dict() for p in (await db.scalars(stmt))]
    # One entry for the whole list, not one per row.
    await audit_phi_read(db, user, None, f"patient list ({len(patients)} records)", request)
    return patients


@router.get("/{patient_id}")
async def get_patient(patient_id: str, request: Request, db: DB, user: AuthUser):
    patient = await db.get(Patient, patient_id)
    if patient is None:
        # Hidden by unit scope (Postgres RLS) is a 403 the UI turns into a break-glass prompt; otherwise 404.
        restricted = await service.restricted_response(db, user, patient_id)
        if restricted is not None:
            return restricted
        raise HTTPException(status_code=404, detail="Patient not found")
    await audit_phi_read(db, user, patient_id, "patient record", request)
    return patient.to_dict()


@router.get("/{patient_id}/encounters")
async def get_encounters(patient_id: str, request: Request, db: DB, user: AuthUser):
    """The patient's stays, newest first, each with its discharge workflows (ADR-002 item 7)."""
    if await db.get(Patient, patient_id) is None:
        restricted = await service.restricted_response(db, user, patient_id)
        if restricted is not None:
            return restricted
        raise HTTPException(status_code=404, detail="Patient not found")
    encounters = await service.encounter_history(db, patient_id)
    await audit_phi_read(db, user, patient_id, f"encounter history ({len(encounters)} records)", request)
    return encounters


@router.post("")
async def create_patient(
    patient_data: PatientCreate, request: Request, db: DB, user: CurrentUser = Depends(requires("patient:write"))
):
    data = patient_data.model_dump()
    data["unit_id"] = data["unit_id"] or None  # "" from a form means no unit
    await service.ensure_unit(db, data["unit_id"])
    patient = Patient(**data)
    db.add(patient)
    await db.flush()
    encounter = await service.open_encounter(db, patient)
    await write_audit(db, user, "CREATE_PATIENT", "patient", patient.patient_id,
                      f"Patient record created, encounter={encounter.encounter_id}", request)
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
    await service.ensure_unit(db, changes.get("unit_id"))
    for field, value in changes.items():
        setattr(patient, field, value)
    await service.sync_encounter(db, patient, set(changes))
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
    for instructions in await db.scalars(select(PatientInstructions).where(PatientInstructions.patient_id == patient_id)):
        instructions.shred()
    for encounter in await db.scalars(select(Encounter).where(Encounter.patient_id == patient_id)):
        encounter.shred()
    await service.redact_grant_reasons(db, patient_id)
    await redact_delay_notes(db, patient_id)
    await write_audit(db, user, "GDPR_ERASURE", "patient", patient_id, "Patient data key destroyed and identifiers erased", request)
    return {"message": "Patient data erased per GDPR right-to-be-forgotten"}


@router.post("/{patient_id}/break-glass")
async def break_glass(patient_id: str, body: BreakGlassRequest, request: Request, db: DB, user: AuthUser):
    """Emergency access to a patient outside your units, for BREAK_GLASS_HOURS. Audited; admins are notified."""
    grant = await service.break_glass(db, user, patient_id, body.reason, request)
    return grant.to_dict()


@access_router.get("")
async def list_access_grants(
    db: DB, active_only: bool = False, limit: int = 100, user: CurrentUser = Depends(requires("audit:read"))
):
    """Break-glass grants for review, newest first, with who used them and the reason they gave."""
    stmt = (select(AccessGrant, User.name).join(User, User.user_id == AccessGrant.user_id)
            .order_by(AccessGrant.created_at.desc()).limit(min(max(limit, 1), 500)))
    if active_only:
        stmt = stmt.where(AccessGrant.expires_at > utcnow())
    now = utcnow()
    return [{**grant.to_dict(), "user_name": name, "active": grant.expires_at > now}
            for grant, name in (await db.execute(stmt)).all()]
