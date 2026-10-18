from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai import service as ai_service
from app.ai.models import AIGeneration, DischargeSummary
from app.ai.prompts import ACTIVE
from app.audit.service import audit_phi_read, write_audit
from app.core.config import settings
from app.core.db import DB
from app.core.security import AuthUser, CurrentUser, requires
from app.discharge import service as discharge_service
from app.discharge.models import Task, Workflow
from app.patients.models import Patient

router = APIRouter(prefix="/ai", tags=["AI"])


class GenerateSummaryRequest(BaseModel):
    patient_id: str
    workflow_id: str
    # Free-text notes from the physician (hospital course, medications, follow-up). De-identified before prompting
    # and never stored; only their hash is kept, inside the generation's input_hash.
    clinical_notes: str = Field(default="", max_length=20000)


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
    summary_id: str, request: Request, db: DB, body: Optional[ApproveSummaryRequest] = None,
    user: CurrentUser = Depends(requires("summary:manage")),
):
    summary = await db.get(DischargeSummary, summary_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    if summary.approved:
        raise HTTPException(status_code=400, detail="Summary already approved")

    edited = ai_service.apply_approval(summary, user, body.content if body else None)

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
                      f"edited={edited} similarity={summary.edit_similarity}", request)
    return {"success": True, "approved_at": summary.approved_at.isoformat(), "edited": edited,
            "edit_similarity": summary.edit_similarity}


@router.get("/summary/{workflow_id}")
async def get_summary(workflow_id: str, request: Request, db: DB, user: AuthUser):
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id))
    if not summary:
        return None
    await audit_phi_read(db, user, summary.patient_id, f"discharge summary {summary.summary_id}", request)
    return summary.to_dict()


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
