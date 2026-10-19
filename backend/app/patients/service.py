"""Unit-scoped patient access and break-glass (ADR-002 item 3).

With UNIT_SCOPED_ACCESS on, a scoped user (one whose role lacks `patient:read_all`) sees patients on their own units,
unassigned patients, and patients they hold an unexpired break-glass grant for. On Postgres the `unit_scope` RLS
policies (core/rls.py) enforce this for patients and every table about a patient, so a hidden patient simply isn't
found. SQLite has no RLS, so local development is not scoped (as it is not tenant-isolated either).
"""
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.capacity.beds import bed_vacated
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import Workflow
from app.identity.models import Unit
from app.notifications.service import notify_role
from app.patients.models import REDACTED, AccessGrant, Encounter, Patient, admitted_at_from

BREAK_GLASS_HOURS = 8
RESTRICTED_DETAIL = "This patient is outside your units. Request break-glass access to open the record."


async def ensure_unit(db: AsyncSession, unit_id: Optional[str]) -> None:
    if unit_id and await db.get(Unit, unit_id) is None:
        raise HTTPException(status_code=400, detail="Unknown unit")


async def has_active_grant(db: AsyncSession, user: CurrentUser, patient_id: str) -> bool:
    return bool(await db.scalar(select(AccessGrant.grant_id).where(
        AccessGrant.patient_id == patient_id, AccessGrant.user_id == user.user_id, AccessGrant.expires_at > utcnow())))


async def visible_by_scope(db: AsyncSession, user: CurrentUser, patient: Patient) -> bool:
    """The rule the RLS policy applies, evaluated in Python (used where RLS can't answer: SQLite, decisions)."""
    if not user.unit_scoped or patient.unit_id is None or patient.unit_id in user.unit_ids:
        return True
    return await has_active_grant(db, user, patient.patient_id)


async def exists_outside_scope(db: AsyncSession, user: CurrentUser, patient_id: str) -> bool:
    """For a patient id that RLS did not return: does it exist in the tenant, hidden by unit scope?"""
    if not user.unit_scoped or db.get_bind().dialect.name != "postgresql":
        return False
    return bool(await db.scalar(text("SELECT df_patient_exists(:pid)"), {"pid": patient_id}))


async def restricted_response(db: AsyncSession, user: CurrentUser, patient_id: str) -> Optional[JSONResponse]:
    """A 403 that tells the client break-glass is possible, or None when the patient really doesn't exist."""
    if await exists_outside_scope(db, user, patient_id):
        return JSONResponse(status_code=403, content={"detail": RESTRICTED_DETAIL, "break_glass": True})
    return None


async def break_glass(db: AsyncSession, user: CurrentUser, patient_id: str, reason: str,
                      request: Optional[Request] = None) -> AccessGrant:
    """Grant `user` access to one patient outside their units for BREAK_GLASS_HOURS, audited and sent to admins."""
    if not user.unit_scoped:
        raise HTTPException(status_code=400, detail="Your access is not limited to units")
    patient = await db.get(Patient, patient_id)
    if patient is None and not await exists_outside_scope(db, user, patient_id):
        raise HTTPException(status_code=404, detail="Patient not found")
    if patient is not None and await visible_by_scope(db, user, patient):
        raise HTTPException(status_code=409, detail="You can already open this record")
    now = utcnow()
    grant = AccessGrant(user_id=user.user_id, patient_id=patient_id, reason=reason.strip(), created_at=now,
                        expires_at=now + timedelta(hours=BREAK_GLASS_HOURS))
    db.add(grant)
    await db.flush()
    # The reason stays on the grant (admins review it there); the audit entry names the grant, never free text.
    await write_audit(db, user, "BREAK_GLASS", "patient", patient_id,
                      f"grant={grant.grant_id} hours={BREAK_GLASS_HOURS}", request)
    await notify_role(db, "admin", "Break-Glass Access",
                      f"{user.name} opened a patient record outside their units (grant {grant.grant_id}).", "/admin")
    return grant


# ---------- Encounters (ADR-002 item 7) ----------
#
# The encounter is the record of a stay; the patient's admission_date, room_number and unit_id mirror the current
# one. Invariant: a patient who is not discharged has exactly one active encounter.

async def current_encounter(db: AsyncSession, patient_id: str) -> Optional[Encounter]:
    return await db.scalar(select(Encounter).where(Encounter.patient_id == patient_id, Encounter.status == "active"))


async def open_encounter(db: AsyncSession, patient: Patient, visit_number: Optional[str] = None) -> Encounter:
    """Start a stay from the patient's current admission fields (admission, readmission)."""
    encounter = Encounter(patient=patient, patient_id=patient.patient_id, unit_id=patient.unit_id,
                          room_number=patient.room_number, admitted_at=admitted_at_from(patient.admission_date))
    encounter.visit_number = visit_number
    db.add(encounter)
    await db.flush()
    return encounter


async def ensure_current_encounter(db: AsyncSession, patient: Patient) -> Encounter:
    """The active encounter, opened from the patient's fields if there is none: a patient created by the release
    before migration 0011 while the deploy rolled out."""
    return await current_encounter(db, patient.patient_id) or await open_encounter(db, patient)


async def sync_encounter(db: AsyncSession, patient: Patient, changed: set[str]) -> None:
    """After a manual edit of the patient, keep the current encounter in step with the mirrored fields and status."""
    encounter = await current_encounter(db, patient.patient_id)
    if patient.status == "discharged":
        if encounter is not None:
            encounter.close(utcnow())
            await bed_vacated(db, encounter)
        return
    if encounter is None:
        await open_encounter(db, patient)  # set back from discharged by hand: a new stay
        return
    if "admission_date" in changed:
        encounter.admitted_at = admitted_at_from(patient.admission_date)
    if "room_number" in changed:
        encounter.room_number = patient.room_number
    if "unit_id" in changed:
        encounter.unit_id = patient.unit_id


async def encounter_history(db: AsyncSession, patient_id: str) -> list[dict]:
    """Every stay, newest first, each with its discharge workflows."""
    encounters = list(await db.scalars(select(Encounter).where(Encounter.patient_id == patient_id)
                                       .order_by(Encounter.created_at.desc())))
    workflows: dict[Optional[str], list[dict]] = {}
    for wf in await db.scalars(select(Workflow).where(Workflow.patient_id == patient_id)
                               .order_by(Workflow.started_at.desc())):
        workflows.setdefault(wf.encounter_id, []).append(
            {k: v for k, v in wf.to_dict().items() if k in ("workflow_id", "status", "started_at", "completed_at")})
    return [{**e.to_dict(), "workflows": workflows.get(e.encounter_id, [])} for e in encounters]


async def redact_grant_reasons(db: AsyncSession, patient_id: str) -> None:
    """Erasure: the reasons may name the patient."""
    for grant in await db.scalars(select(AccessGrant).where(AccessGrant.patient_id == patient_id)):
        grant.reason = REDACTED
