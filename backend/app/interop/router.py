"""Read-only FHIR R4 API at /api/fhir/R4 (ADR-001 Phase 5, item 19).

Same authentication and tenant isolation as the rest of the API, and every read is written to the audit chain,
because these responses carry PHI out of the system.
"""
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.ai.models import DischargeSummary
from app.audit.service import write_audit
from app.core.config import VERSION
from app.core.db import DB
from app.core.security import AuthUser
from app.discharge.models import Task, Workflow
from app.interop import fhir
from app.patients.models import Encounter, Patient

router = APIRouter(prefix="/fhir/R4", tags=["FHIR R4"])

MAX_COUNT = 200


def _fhir(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, media_type=fhir.FHIR_JSON)


def _not_found(resource_type: str, rid: str) -> JSONResponse:
    return _fhir(fhir.operation_outcome("not-found", f"{resource_type}/{rid} not found"), 404)


def _base_url(request: Request) -> str:
    return str(request.url_for("fhir_metadata")).removesuffix("metadata")


def _patient_id(ref: Optional[str]) -> Optional[str]:
    return ref.removeprefix("Patient/") if ref else None


def _encounter_id(ref: Optional[str]) -> Optional[str]:
    return ref.removeprefix("Encounter/") if ref else None


async def _audit(db, user, request: Request, what: str, count: int) -> None:
    await write_audit(db, user, "FHIR_READ", "fhir", None, f"{what} ({count} resource(s))"[:500], request)


@router.get("/metadata", name="fhir_metadata")
async def metadata(request: Request):
    return _fhir(fhir.capability_statement(_base_url(request), VERSION))


# ---------- Patient ----------

@router.get("/Patient/{patient_id}")
async def read_patient(patient_id: str, request: Request, db: DB, user: AuthUser):
    p = await db.get(Patient, patient_id)
    if not p:
        return _not_found("Patient", patient_id)
    await _audit(db, user, request, f"Patient/{patient_id}", 1)
    return _fhir(fhir.patient(p))


@router.get("/Patient")
async def search_patients(request: Request, db: DB, user: AuthUser, _id: Optional[str] = None,
                          _count: int = Query(50, ge=1, le=MAX_COUNT)):
    stmt = select(Patient).order_by(Patient.created_at).limit(_count)
    if _id:
        stmt = stmt.where(Patient.patient_id == _id)
    resources = [fhir.patient(p) for p in await db.scalars(stmt)]
    await _audit(db, user, request, f"Patient?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))


# ---------- Encounter (one inpatient stay) ----------

async def _workflow_encounters(db, workflow_ids: set) -> dict:
    """workflow_id -> encounter_id, for the Encounter references of tasks and documents."""
    ids = {w for w in workflow_ids if w}
    if not ids:
        return {}
    return dict((await db.execute(
        select(Workflow.workflow_id, Workflow.encounter_id).where(Workflow.workflow_id.in_(ids)))).all())


def _encounter_workflows(encounter: str):
    return select(Workflow.workflow_id).where(Workflow.encounter_id == _encounter_id(encounter))


@router.get("/Encounter/{encounter_id}")
async def read_encounter(encounter_id: str, request: Request, db: DB, user: AuthUser):
    e = await db.get(Encounter, encounter_id)
    if not e:
        return _not_found("Encounter", encounter_id)
    await _audit(db, user, request, f"Encounter/{encounter_id}", 1)
    return _fhir(fhir.encounter(e))


@router.get("/Encounter")
async def search_encounters(request: Request, db: DB, user: AuthUser, _id: Optional[str] = None,
                            patient: Optional[str] = None, _count: int = Query(50, ge=1, le=MAX_COUNT)):
    stmt = select(Encounter).order_by(Encounter.created_at.desc()).limit(_count)
    if _id:
        stmt = stmt.where(Encounter.encounter_id == _id)
    if patient:
        stmt = stmt.where(Encounter.patient_id == _patient_id(patient))
    resources = [fhir.encounter(e) for e in await db.scalars(stmt)]
    await _audit(db, user, request, f"Encounter?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))


# ---------- Task ----------

@router.get("/Task/{task_id}")
async def read_task(task_id: str, request: Request, db: DB, user: AuthUser):
    t = await db.get(Task, task_id)
    if not t:
        return _not_found("Task", task_id)
    await _audit(db, user, request, f"Task/{task_id}", 1)
    encounters = await _workflow_encounters(db, {t.workflow_id})
    return _fhir(fhir.task(t, encounters.get(t.workflow_id)))


@router.get("/Task")
async def search_tasks(request: Request, db: DB, user: AuthUser, _id: Optional[str] = None,
                       patient: Optional[str] = None, encounter: Optional[str] = None,
                       _count: int = Query(50, ge=1, le=MAX_COUNT)):
    stmt = select(Task).order_by(Task.priority, Task.created_at).limit(_count)
    if _id:
        stmt = stmt.where(Task.task_id == _id)
    if patient:
        stmt = stmt.where(Task.patient_id == _patient_id(patient))
    if encounter:
        stmt = stmt.where(Task.workflow_id.in_(_encounter_workflows(encounter)))
    tasks = list(await db.scalars(stmt))
    encounters = await _workflow_encounters(db, {t.workflow_id for t in tasks})
    resources = [fhir.task(t, encounters.get(t.workflow_id)) for t in tasks]
    await _audit(db, user, request, f"Task?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))


# ---------- DocumentReference (approved discharge summaries only) ----------

def _approved():
    return select(DischargeSummary).where(DischargeSummary.approved.is_(True))


@router.get("/DocumentReference/{summary_id}")
async def read_document(summary_id: str, request: Request, db: DB, user: AuthUser):
    s = await db.scalar(_approved().where(DischargeSummary.summary_id == summary_id))
    if not s:  # drafts are indistinguishable from missing: they are not documents yet
        return _not_found("DocumentReference", summary_id)
    await _audit(db, user, request, f"DocumentReference/{summary_id}", 1)
    encounters = await _workflow_encounters(db, {s.workflow_id})
    return _fhir(fhir.document_reference(s, encounters.get(s.workflow_id)))


@router.get("/DocumentReference")
async def search_documents(request: Request, db: DB, user: AuthUser, _id: Optional[str] = None,
                           patient: Optional[str] = None, encounter: Optional[str] = None,
                           _count: int = Query(50, ge=1, le=MAX_COUNT)):
    stmt = _approved().order_by(DischargeSummary.approved_at.desc()).limit(_count)
    if _id:
        stmt = stmt.where(DischargeSummary.summary_id == _id)
    if patient:
        stmt = stmt.where(DischargeSummary.patient_id == _patient_id(patient))
    if encounter:
        stmt = stmt.where(DischargeSummary.workflow_id.in_(_encounter_workflows(encounter)))
    summaries = list(await db.scalars(stmt))
    encounters = await _workflow_encounters(db, {s.workflow_id for s in summaries})
    resources = [fhir.document_reference(s, encounters.get(s.workflow_id)) for s in summaries]
    await _audit(db, user, request, f"DocumentReference?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))
