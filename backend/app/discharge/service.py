"""Discharge workflow state machine: in_progress -> pending_approval -> completed."""
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AIGeneration, DischargeSummary
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import ACTIVE_WORKFLOW_STATUSES, Task, Workflow
from app.notifications.service import notify_role
from app.patients.models import Patient

STANDARD_TASKS = [
    {"title": "Complete Discharge Summary", "description": "Generate and approve AI discharge summary", "category": "documentation", "assigned_role": "physician", "priority": 1},
    {"title": "Final Medication Review", "description": "Review and reconcile all medications", "category": "medication", "assigned_role": "pharmacist", "priority": 1},
    {"title": "Prepare Discharge Medications", "description": "Prepare and package medications for patient", "category": "medication", "assigned_role": "pharmacist", "priority": 2},
    {"title": "Patient Education", "description": "Provide discharge instructions and education materials", "category": "education", "assigned_role": "nurse", "priority": 2},
    {"title": "Schedule Follow-up Appointments", "description": "Schedule necessary follow-up appointments", "category": "follow_up", "assigned_role": "nurse", "priority": 2},
    {"title": "Billing Clearance", "description": "Verify insurance and billing clearance", "category": "billing", "assigned_role": "nurse", "priority": 3},
    {"title": "Final Vitals Check", "description": "Record final vital signs before discharge", "category": "documentation", "assigned_role": "nurse", "priority": 1},
    {"title": "Remove IV and Equipment", "description": "Remove IV lines and return equipment", "category": "documentation", "assigned_role": "nurse", "priority": 2},
]
SUMMARY_TASK_TITLE = STANDARD_TASKS[0]["title"]


async def active_workflow_for(db: AsyncSession, patient_id: str) -> Optional[Workflow]:
    return await db.scalar(
        select(Workflow).where(Workflow.patient_id == patient_id, Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES))
    )


async def workflow_detail(db: AsyncSession, workflow: Workflow) -> dict:
    """Workflow plus its tasks (priority order) and discharge summary, as the UI expects."""
    tasks = await db.scalars(
        select(Task).where(Task.workflow_id == workflow.workflow_id).order_by(Task.priority, Task.created_at)
    )
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow.workflow_id))
    out = workflow.to_dict()
    out["tasks"] = [t.to_dict() for t in tasks]
    out["discharge_summary"] = summary.to_dict() if summary else None
    if summary and summary.generation_id:
        gen = await db.get(AIGeneration, summary.generation_id)
        out["discharge_summary"]["generation"] = {
            k: getattr(gen, k) for k in ("provider", "model_id", "prompt_version", "grounding_warnings", "attempts")
        } if gen else None
    return out


async def open_task_count(db: AsyncSession, workflow_id: str) -> int:
    return await db.scalar(
        select(func.count()).select_from(Task).where(Task.workflow_id == workflow_id, Task.status != "completed")
    )


async def initiate_discharge(db: AsyncSession, patient_id: str, user: CurrentUser) -> dict:
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if await active_workflow_for(db, patient_id):
        raise HTTPException(status_code=400, detail="Active workflow already exists")

    workflow = Workflow(
        patient_id=patient_id,
        initiated_by=user.user_id,
        estimated_discharge_time=utcnow() + timedelta(hours=2),
    )
    db.add(workflow)
    try:
        # The partial unique index catches two concurrent initiations that both passed the check above.
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=400, detail="Active workflow already exists")

    db.add_all(Task(workflow_id=workflow.workflow_id, patient_id=patient_id, **t) for t in STANDARD_TASKS)
    patient.status = "pending_discharge"

    await notify_role(db, "nurse", "New Discharge Initiated",
                      f"Discharge workflow started for patient {patient.name}", f"/patients/{patient_id}")
    await notify_role(db, "pharmacist", "Medication Review Required",
                      f"Medication review needed for {patient.name}'s discharge", f"/patients/{patient_id}")
    await db.flush()
    return {"workflow": workflow.to_dict(), "tasks_created": len(STANDARD_TASKS)}


async def create_task(db: AsyncSession, user: CurrentUser, *, patient_id: str, title: str, description: str,
                      category: str, assigned_role: str, priority: int = 2) -> Task:
    """A manual task, attached to the patient's active workflow if there is one."""
    if not await db.get(Patient, patient_id):
        raise HTTPException(status_code=404, detail="Patient not found")
    workflow = await active_workflow_for(db, patient_id)
    task = Task(patient_id=patient_id, title=title, description=description, category=category,
                assigned_role=assigned_role, priority=priority,
                workflow_id=workflow.workflow_id if workflow else None, created_by=user.user_id)
    db.add(task)
    await db.flush()
    # A new open task sends a pending_approval workflow back to in_progress.
    await sync_workflow_status(db, task.workflow_id)
    return task


async def update_task(db: AsyncSession, user: CurrentUser, task_id: str, *, status: Optional[str] = None,
                      assigned_to: Optional[str] = None) -> Task:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if status:
        task.status = status
        if status == "completed":
            task.completed_by = user.user_id
            task.completed_at = utcnow()
        else:
            task.completed_by = None
            task.completed_at = None
    if assigned_to:
        task.assigned_to = assigned_to
    await sync_workflow_status(db, task.workflow_id)
    await db.flush()
    await db.refresh(task)
    return task


async def sync_workflow_status(db: AsyncSession, workflow_id: Optional[str]) -> None:
    """Move an active workflow between in_progress and pending_approval based on its open tasks."""
    if not workflow_id:
        return
    workflow = await db.get(Workflow, workflow_id)
    if not workflow or workflow.status not in ACTIVE_WORKFLOW_STATUSES:
        return
    await db.flush()
    open_tasks = await open_task_count(db, workflow_id)
    if open_tasks == 0 and workflow.status == "in_progress":
        workflow.status = "pending_approval"
        await notify_role(db, "physician", "Discharge Ready for Approval",
                          "All tasks completed for patient. Ready for final discharge approval.",
                          f"/workflows/{workflow_id}")
    elif open_tasks > 0 and workflow.status == "pending_approval":
        workflow.status = "in_progress"


async def complete_discharge(db: AsyncSession, workflow_id: str) -> dict:
    workflow = await db.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.status not in ACTIVE_WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"Workflow is already {workflow.status}")

    pending = await open_task_count(db, workflow_id)
    if pending > 0:
        raise HTTPException(status_code=400, detail=f"{pending} tasks still pending")

    approved = await db.scalar(
        select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id, DischargeSummary.approved.is_(True))
    )
    if not approved:
        raise HTTPException(status_code=400, detail="Discharge summary not approved")

    now = utcnow()
    workflow.status = "completed"
    workflow.completed_at = now
    patient = await db.get(Patient, workflow.patient_id)
    if patient:
        patient.status = "discharged"
    return {"success": True, "completed_at": now.isoformat()}
