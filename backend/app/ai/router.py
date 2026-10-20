from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import instructions as instructions_service
from app.ai import lifecycle
from app.ai import service as ai_service
from app.ai.models import AIGeneration, DischargeSummary, PatientInstructions
from app.ai.prompts import ACTIVE
from app.audit.service import audit_phi_read, write_audit
from app.core.config import settings
from app.core.db import DB, get_session_factory
from app.core.security import AuthUser, CurrentUser, requires
from app.delivery import service as delivery_service
from app.discharge import service as discharge_service
from app.discharge.models import Task, Workflow
from app.identity.models import User
from app.patients.models import LANGUAGE_TAG_PATTERN, Patient

router = APIRouter(prefix="/ai", tags=["AI"])
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class GenerateSummaryRequest(BaseModel):
    patient_id: str
    workflow_id: str
    # Free-text notes from the physician (hospital course, medications, follow-up). De-identified before prompting
    # and never stored; only their hash is kept, inside the generation's input_hash.
    clinical_notes: str = Field(default="", max_length=20000)


class AddendumRequest(BaseModel):
    content: str = Field(min_length=1, max_length=lifecycle.MAX_ADDENDUM_CHARS)


class ApproveSummaryRequest(BaseModel):
    # The physician's edited text. Omitted or blank: approve the draft as generated.
    content: Optional[str] = Field(default=None, max_length=50000)


@router.post("/generate-summary")
async def generate_discharge_summary(
    req: GenerateSummaryRequest, request: Request, db: DB, user: CurrentUser = Depends(requires("summary:manage"))
):
    patient = await db.get(Patient, req.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    workflow = await db.get(Workflow, req.workflow_id)
    if not workflow or workflow.patient_id != patient.patient_id:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == req.workflow_id)):
        raise HTTPException(status_code=400, detail="Summary already exists for this workflow")

    result = await ai_service.generate_discharge_summary(db, user, patient, workflow, req.clinical_notes)
    gen = result.generation
    await write_audit(db, user, "AI_SUMMARY_GENERATED" if result.doc else "AI_SUMMARY_FAILED", "discharge_summary",
                      gen.generation_id, f"{gen.prompt_version} {gen.model_id} status={gen.status} "
                      f"warnings={len(gen.grounding_warnings)}", request)
    if result.doc is None:
        # Returned, not raised, so the failed attempt's provenance and audit rows commit.
        return JSONResponse(status_code=502, content={"detail": "AI summary generation failed; nothing was saved. "
                                                                "Try again or write the summary manually.",
                                                      "generation_id": gen.generation_id, "status": gen.status})
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == req.workflow_id))
    return {**summary.to_dict(), "generation": gen.to_dict()}


@router.put("/approve-summary/{summary_id}")
async def approve_summary(
    summary_id: str, request: Request, db: DB, background_tasks: BackgroundTasks, factory: SessionFactory,
    body: Optional[ApproveSummaryRequest] = None, user: CurrentUser = Depends(requires("summary:manage")),
):
    summary = await db.get(DischargeSummary, summary_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    if summary.approved:
        raise HTTPException(status_code=400, detail="Summary already approved")

    edited = ai_service.apply_approval(summary, user, body.content if body else None)
    version = lifecycle.sign(summary, user)

    # Approving the summary completes the workflow's documentation task.
    task = await db.scalar(
        select(Task).where(
            Task.workflow_id == summary.workflow_id, Task.title == discharge_service.SUMMARY_TASK_TITLE
        )
    )
    if task and task.status != "completed":
        task.status = "completed"
        task.completed_by = user.user_id
        task.completed_at = summary.approved_at
    await discharge_service.sync_workflow_status(db, summary.workflow_id)
    await write_audit(db, user, "AI_SUMMARY_APPROVED", "discharge_summary", summary_id,
                      f"signed version={version.version_id} edited={edited} similarity={summary.edit_similarity}",
                      request)
    # ADR-002 item 16: queue the signed summary for the stay's recipients in this transaction; the first attempt
    # runs after commit.
    queued = await delivery_service.enqueue_for_summary(db, user, summary, version.version_id)
    delivery_service.schedule(background_tasks, factory, user.tenant_id, queued)
    return {"success": True, "approved_at": summary.approved_at.isoformat(), "edited": edited,
            "edit_similarity": summary.edit_similarity, "version_id": version.version_id,
            "version": version.version, "content_sha256": version.content_sha256}


@router.post("/summaries/{summary_id}/addenda", status_code=201)
async def add_addendum(summary_id: str, body: AddendumRequest, request: Request, db: DB,
                       background_tasks: BackgroundTasks, factory: SessionFactory,
                       user: CurrentUser = Depends(requires("summary:manage"))):
    """Add signed text to a signed summary. The signed version itself never changes."""
    summary = await db.get(DischargeSummary, summary_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    addendum = lifecycle.add_addendum(summary, user, body.content)
    await db.flush()
    await write_audit(db, user, "SUMMARY_ADDENDUM_SIGNED", "discharge_summary", summary_id,
                      f"addendum={addendum.addendum_id} version={addendum.version_id}", request)
    # Receivers of the signed summary get the amended document.
    queued = await delivery_service.enqueue_for_summary(db, user, summary, addendum.addendum_id)
    delivery_service.schedule(background_tasks, factory, user.tenant_id, queued)
    return lifecycle.addendum_dict(addendum)


@router.get("/summaries/overdue")
async def overdue_summaries(db: DB, user: CurrentUser = Depends(requires("summary:manage"))):
    """Discharges whose summary is unsigned past the sign-by deadline (ids and times only)."""
    return {"sign_by_hours": settings.summary_sign_hours, "overdue": await lifecycle.unsigned_overdue(db)}


@router.post("/summaries/overdue/notify")
async def notify_overdue_summaries(db: DB, user: CurrentUser = Depends(requires("summary:manage"))):
    """Notify physicians about overdue summaries once each (what `app.cli summary-deadlines` runs on a schedule)."""
    return {"notified": await lifecycle.notify_overdue(db)}


@router.get("/summary/{workflow_id}")
async def get_summary(workflow_id: str, request: Request, db: DB, user: AuthUser):
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id))
    if not summary:
        return None
    workflow = await db.get(Workflow, workflow_id)
    await audit_phi_read(db, user, summary.patient_id, f"discharge summary {summary.summary_id}", request)
    return lifecycle.summary_view(summary, workflow)


class GenerateInstructionsRequest(BaseModel):
    workflow_id: str
    # BCP 47 code. Omitted: the patient's preferred language, else English.
    language: Optional[str] = Field(default=None, pattern=LANGUAGE_TAG_PATTERN)


class ApproveInstructionsRequest(BaseModel):
    # The reviewer's edited text. Omitted or blank: approve the draft as generated.
    content: Optional[str] = Field(default=None, max_length=50000)


async def _instructions_or_404(db, instructions_id: str) -> PatientInstructions:
    row = await db.get(PatientInstructions, instructions_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instructions not found")
    return row


@router.post("/patient-instructions")
async def generate_patient_instructions(req: GenerateInstructionsRequest, request: Request, db: DB,
                                        user: CurrentUser = Depends(requires("instructions:manage"))):
    """Draft patient-facing instructions from the signed summary, in the patient's language (ADR-002 item 18)."""
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == req.workflow_id))
    if summary is None:
        raise HTTPException(status_code=404, detail="No discharge summary for this workflow")
    language = req.language or summary.patient.preferred_language or instructions_service.DEFAULT_LANGUAGE
    result, row = await instructions_service.generate(db, user, summary, language)
    gen = result.generation
    await write_audit(db, user, "AI_INSTRUCTIONS_GENERATED" if row else "AI_INSTRUCTIONS_FAILED",
                      "patient_instructions", row.instructions_id if row else gen.generation_id,
                      f"{gen.prompt_version} {gen.model_id} status={gen.status} language={language} "
                      f"warnings={len(gen.grounding_warnings)} generation={gen.generation_id}", request)
    if row is None:
        # Returned, not raised, so the failed attempt's provenance and audit rows commit.
        return JSONResponse(status_code=502, content={"detail": "Drafting the patient instructions failed; nothing "
                                                                "was saved. Try again or write them by hand.",
                                                      "generation_id": gen.generation_id, "status": gen.status})
    return instructions_service.view(row, summary, gen)


@router.get("/patient-instructions")
async def list_patient_instructions(workflow_id: str, request: Request, db: DB, user: AuthUser):
    """The workflow's patient instructions (one per language), with provenance and warnings."""
    rows = list(await db.scalars(select(PatientInstructions).where(PatientInstructions.workflow_id == workflow_id)
                                 .order_by(PatientInstructions.created_at)))
    if not rows:
        return []
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id))
    generations = {g.generation_id: g for g in await db.scalars(select(AIGeneration).where(
        AIGeneration.generation_id.in_([r.generation_id for r in rows if r.generation_id])))}
    await audit_phi_read(db, user, rows[0].patient_id, f"patient instructions ({len(rows)} records)", request)
    return [instructions_service.view(r, summary, generations.get(r.generation_id or "")) for r in rows]


@router.put("/patient-instructions/{instructions_id}/approve")
async def approve_patient_instructions(instructions_id: str, request: Request, db: DB,
                                       body: Optional[ApproveInstructionsRequest] = None,
                                       user: CurrentUser = Depends(requires("instructions:manage"))):
    row = await _instructions_or_404(db, instructions_id)
    edited = instructions_service.approve(row, user, body.content if body else None)
    await write_audit(db, user, "PATIENT_INSTRUCTIONS_APPROVED", "patient_instructions", instructions_id,
                      f"language={row.language} edited={edited} similarity={row.edit_similarity} "
                      f"grade={row.approved_reading_grade}", request)
    return {"success": True, "approved_at": row.approved_at.isoformat(), "edited": edited,
            "edit_similarity": row.edit_similarity, "approved_reading_grade": row.approved_reading_grade}


@router.get("/patient-instructions/{instructions_id}/print")
async def print_patient_instructions(instructions_id: str, request: Request, db: DB,
                                     user: CurrentUser = Depends(requires("instructions:manage"))):
    """The approved handout with the patient's name, for printing. Drafts can't be printed."""
    row = await _instructions_or_404(db, instructions_id)
    if not row.approved:
        raise HTTPException(status_code=409, detail="Only approved instructions can be printed")
    if row.patient.gdpr_erased:
        raise HTTPException(status_code=410, detail="The patient's record was erased")
    approver = await db.get(User, row.approved_by) if row.approved_by else None
    summary = await db.get(DischargeSummary, row.summary_id)
    await audit_phi_read(db, user, row.patient_id, f"patient instructions {instructions_id} printed", request)
    return {"instructions_id": row.instructions_id, "workflow_id": row.workflow_id, "patient_id": row.patient_id,
            "patient_name": row.patient.name, "language": row.language,
            "language_name": instructions_service.language_name(row.language), "content": row.content,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "approved_by_name": approver.name if approver else None,
            "stale": instructions_service.view(row, summary)["stale"]}


@router.get("/generations")
async def list_generations(db: DB, workflow_id: Optional[str] = None, limit: int = 50,
                           user: CurrentUser = Depends(requires("summary:manage"))):
    """Provenance: which model and prompt version produced each draft, at what cost."""
    stmt = select(AIGeneration).order_by(AIGeneration.created_at.desc()).limit(min(limit, 200))
    if workflow_id:
        stmt = stmt.where(AIGeneration.workflow_id == workflow_id)
    return [g.to_dict() for g in await db.scalars(stmt)]


@router.get("/metrics")
async def ai_metrics(db: DB, user: CurrentUser = Depends(requires("summary:manage"))):
    return {"provider": settings.llm_provider, "active_prompts": ACTIVE,
            "summary_quality": await ai_service.quality_metrics(db)}
