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
from app.patients.models import Patient

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


# ---------- Encounter (discharge workflow) ----------

async def _encounters(db, stmt) -> list[dict]:
    workflows = list(await db.scalars(stmt))
    patients = {p.patient_id: p for p in await db.scalars(
        select(Patient).where(Patient.patient_id.in_({w.patient_id for w in workflows})))} if workflows else {}
    return [fhir.encounter(w, patients.get(w.patient_id)) for w in workflows]


@router.get("/Encounter/{workflow_id}")
async def read_encounter(workflow_id: str, request: Request, db: DB, user: AuthUser):
    resources = await _encounters(db, select(Workflow).where(Workflow.workflow_id == workflow_id))
    if not resources:
        return _not_found("Encounter", workflow_id)
    await _audit(db, user, request, f"Encounter/{workflow_id}", 1)
    return _fhir(resources[0])


@router.get("/Encounter")
async def search_encounters(request: Request, db: DB, user: AuthUser, _id: Optional[str] = None,
                            patient: Optional[str] = None, _count: int = Query(50, ge=1, le=MAX_COUNT)):
    stmt = select(Workflow).order_by(Workflow.started_at.desc()).limit(_count)
    if _id:
        stmt = stmt.where(Workflow.workflow_id == _id)
    if patient:
        stmt = stmt.where(Workflow.patient_id == _patient_id(patient))
    resources = await _encounters(db, stmt)
    await _audit(db, user, request, f"Encounter?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))


# ---------- Task ----------

@router.get("/Task/{task_id}")
async def read_task(task_id: str, request: Request, db: DB, user: AuthUser):
    t = await db.get(Task, task_id)
    if not t:
        return _not_found("Task", task_id)
    await _audit(db, user, request, f"Task/{task_id}", 1)
    return _fhir(fhir.task(t))


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
        stmt = stmt.where(Task.workflow_id == _encounter_id(encounter))
    resources = [fhir.task(t) for t in await db.scalars(stmt)]
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
    return _fhir(fhir.document_reference(s))


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
        stmt = stmt.where(DischargeSummary.workflow_id == _encounter_id(encounter))
    resources = [fhir.document_reference(s) for s in await db.scalars(stmt)]
    await _audit(db, user, request, f"DocumentReference?{request.url.query}", len(resources))
    return _fhir(fhir.bundle(resources, _base_url(request)))
