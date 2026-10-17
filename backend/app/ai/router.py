from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import service as ai_service
from app.ai.models import DischargeSummary
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, requires
from app.discharge import service as discharge_service
from app.discharge.models import Task, Workflow
from app.patients.models import Patient

router = APIRouter(prefix="/ai", tags=["AI"])


class GenerateSummaryRequest(BaseModel):
    patient_id: str
    workflow_id: str


@router.post("/generate-summary")
async def generate_discharge_summary(
    req: GenerateSummaryRequest, db: DB, user: CurrentUser = Depends(requires("summary:manage"))
):
    patient = await db.get(Patient, req.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    workflow = await db.get(Workflow, req.workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == req.workflow_id)):
        raise HTTPException(status_code=400, detail="Summary already exists for this workflow")

    content = await ai_service.generate_summary_text(
        ai_service.build_summary_prompt(patient), session_id=f"discharge_{req.workflow_id}"
    )
    summary = DischargeSummary(workflow_id=req.workflow_id, patient_id=req.patient_id, content=content, generated_by="ai")
    db.add(summary)
    await db.flush()
    return summary.to_dict()


@router.put("/approve-summary/{summary_id}")
async def approve_summary(summary_id: str, db: DB, user: CurrentUser = Depends(requires("summary:manage"))):
    summary = await db.get(DischargeSummary, summary_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")

    now = utcnow()
    summary.approved = True
    summary.approved_by = user.user_id
    summary.approved_at = now

    # Approving the summary completes the workflow's documentation task.
    task = await db.scalar(
        select(Task).where(
            Task.workflow_id == summary.workflow_id, Task.title == discharge_service.SUMMARY_TASK_TITLE
        )
    )
    if task and task.status != "completed":
        task.status = "completed"
        task.completed_by = user.user_id
        task.completed_at = now
    await discharge_service.sync_workflow_status(db, summary.workflow_id)
    return {"success": True, "approved_at": now.isoformat()}


@router.get("/summary/{workflow_id}")
async def get_summary(workflow_id: str, db: DB, user: AuthUser):
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id))
    return summary.to_dict() if summary else None
