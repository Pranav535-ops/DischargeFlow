from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.audit.service import audit_phi_read
from app.core.db import DB
from app.core.security import AuthUser, CurrentUser, requires
from app.discharge import service
from app.discharge.models import Task, Workflow

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
    # Required when changing the status of a task assigned to another user or role (task:override).
    reason: Optional[str] = Field(default=None, max_length=500)


# ---------- workflows ----------

@workflows_router.post("/initiate/{patient_id}")
async def initiate_discharge(
    patient_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("workflow:manage"))
):
    return await service.initiate_discharge(db, patient_id, user, request)


@workflows_router.get("")
async def get_workflows(db: DB, user: AuthUser, status: Optional[str] = None):
    stmt = select(Workflow).order_by(Workflow.started_at.desc())
    if status:
        stmt = stmt.where(Workflow.status == status)
    return [w.to_dict() for w in (await db.scalars(stmt))]


@workflows_router.get("/patient/{patient_id}")
async def get_patient_workflow(patient_id: str, request: Request, db: DB, user: AuthUser):
    workflow = await service.active_workflow_for(db, patient_id)
    if not workflow:
        return None
    detail = await service.workflow_detail(db, workflow)
    # Audit last: the entry locks the tenant's audit chain head until the request commits.
    await audit_phi_read(db, user, patient_id, f"workflow {workflow.workflow_id} (active, with tasks and summary)",
                         request)
    return detail


@workflows_router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request, db: DB, user: AuthUser):
    workflow = await db.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    detail = await service.workflow_detail(db, workflow)
    await audit_phi_read(db, user, workflow.patient_id, f"workflow {workflow_id} (with tasks and summary)", request)
    return detail


@workflows_router.put("/{workflow_id}/complete")
async def complete_workflow(
    workflow_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("workflow:manage"))
):
    return await service.complete_discharge(db, workflow_id, user, request)


# ---------- tasks ----------

@tasks_router.post("")
async def create_task(
    task_data: TaskCreate, db: DB, user: CurrentUser = Depends(requires("task:create"))
):
    """Create a manual task, attached to the patient's active workflow if there is one."""
    task = await service.create_task(db, user, **task_data.model_dump())
    return task.to_dict()


@tasks_router.get("/my")
async def get_my_tasks(db: DB, user: AuthUser):
    stmt = (
        service.visible_tasks_for(user)
        .where(Task.status != "completed")
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
async def update_task(task_id: str, task_update: TaskUpdate, request: Request, db: DB, user: AuthUser):
    """The assignee changes the status; anyone else needs task:override and a reason. Naming an assignee needs
    task:assign, unless you claim a task of your own role."""
    task = await service.update_task(db, user, task_id, status=task_update.status,
                                     assigned_to=task_update.assigned_to, reason=task_update.reason,
                                     request=request)
    return task.to_dict()


