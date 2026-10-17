from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, requires
from app.discharge import service
from app.discharge.models import Task, Workflow
from app.patients.models import Patient

workflows_router = APIRouter(prefix="/workflows", tags=["Workflows"])
tasks_router = APIRouter(prefix="/tasks", tags=["Tasks"])

TaskStatus = Literal["pending", "in_progress", "completed"]


class TaskCreate(BaseModel):
    patient_id: str
    title: str
    description: str
    category: str
    assigned_role: str
    priority: int = 2


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    assigned_to: Optional[str] = None


# ---------- workflows ----------

@workflows_router.post("/initiate/{patient_id}")
async def initiate_discharge(
    patient_id: str, db: DB, user: CurrentUser = Depends(requires("workflow:manage"))
):
    return await service.initiate_discharge(db, patient_id, user)


@workflows_router.get("")
async def get_workflows(db: DB, user: AuthUser, status: Optional[str] = None):
    stmt = select(Workflow).order_by(Workflow.started_at.desc())
    if status:
        stmt = stmt.where(Workflow.status == status)
    return [w.to_dict() for w in (await db.scalars(stmt))]


@workflows_router.get("/patient/{patient_id}")
async def get_patient_workflow(patient_id: str, db: DB, user: AuthUser):
    workflow = await service.active_workflow_for(db, patient_id)
    return await service.workflow_detail(db, workflow) if workflow else None


@workflows_router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, db: DB, user: AuthUser):
    workflow = await db.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return await service.workflow_detail(db, workflow)


@workflows_router.put("/{workflow_id}/complete")
async def complete_workflow(
    workflow_id: str, db: DB, user: CurrentUser = Depends(requires("workflow:manage"))
):
    return await service.complete_discharge(db, workflow_id)


# ---------- tasks ----------

@tasks_router.post("")
async def create_task(
    task_data: TaskCreate, db: DB, user: CurrentUser = Depends(requires("task:create"))
):
    """Create a manual task, attached to the patient's active workflow if there is one."""
    if not await db.get(Patient, task_data.patient_id):
        raise HTTPException(status_code=404, detail="Patient not found")
    workflow = await service.active_workflow_for(db, task_data.patient_id)
    task = Task(
        **task_data.model_dump(),
        workflow_id=workflow.workflow_id if workflow else None,
        created_by=user.user_id,
    )
    db.add(task)
    await db.flush()
    # A new open task sends a pending_approval workflow back to in_progress.
    await service.sync_workflow_status(db, task.workflow_id)
    return task.to_dict()


@tasks_router.get("/my")
async def get_my_tasks(db: DB, user: AuthUser):
    stmt = (
        select(Task)
        .where(Task.assigned_role == user.role, Task.status != "completed")
        .order_by(Task.priority, Task.created_at)
        .limit(100)
    )
    return [t.to_dict() for t in (await db.scalars(stmt))]


@tasks_router.get("")
async def get_tasks(db: DB, user: AuthUser, role: Optional[str] = None, status: Optional[str] = None):
    stmt = select(Task).order_by(Task.priority, Task.created_at)
    if role:
        stmt = stmt.where(Task.assigned_role == role)
    if status:
        stmt = stmt.where(Task.status == status)
    return [t.to_dict() for t in (await db.scalars(stmt))]


@tasks_router.put("/{task_id}")
async def update_task(task_id: str, task_update: TaskUpdate, db: DB, user: AuthUser):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task_update.status:
        task.status = task_update.status
        if task_update.status == "completed":
            task.completed_by = user.user_id
            task.completed_at = utcnow()
        else:
            task.completed_by = None
            task.completed_at = None
    if task_update.assigned_to:
        task.assigned_to = task_update.assigned_to

    await service.sync_workflow_status(db, task.workflow_id)
    await db.flush()
    await db.refresh(task)
    return task.to_dict()


def visible_tasks_for(user: CurrentUser):
    """Tasks assigned to the user directly or to their role (used by the chatbot)."""
    return select(Task).where(or_(Task.assigned_to == user.user_id, Task.assigned_role == user.role))
