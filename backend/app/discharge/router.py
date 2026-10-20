from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select

from app.audit.service import audit_phi_read
from app.core.db import DB
from app.core.security import AuthUser, CurrentUser, ensure_permission, requires
from app.discharge import planning, service, templates
from app.discharge.models import DELAY_CODES, DISPOSITIONS, Task, Workflow
from app.identity.permissions import ROLES
from app.patients.models import Encounter
from app.patients.service import ensure_not_erased
from app.risk import lace
from app.risk import service as risk

workflows_router = APIRouter(prefix="/workflows", tags=["Workflows"])
tasks_router = APIRouter(prefix="/tasks", tags=["Tasks"])
plans_router = APIRouter(tags=["Discharge plans"])
templates_router = APIRouter(prefix="/workflow-templates", tags=["Workflow templates"])

TaskStatus = Literal["pending", "in_progress", "completed"]
DelayCode = Literal[DELAY_CODES]  # type: ignore[valid-type]
Disposition = Literal[DISPOSITIONS]  # type: ignore[valid-type]


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
    # Required when completing a task after its due time, unless one was recorded already (ADR-002 item 10).
    delay_code: Optional[DelayCode] = None
    delay_note: Optional[str] = Field(default=None, max_length=500)


class DelayIn(BaseModel):
    code: DelayCode
    # Optional except for `other`. Kept on the delay row only (never audited); erasure redacts it.
    note: Optional[str] = Field(default=None, max_length=500)


class PlanUpdate(BaseModel):
    """Fields left out stay as they are; null clears one."""
    expected_discharge_date: Optional[date] = None
    planned_disposition: Optional[Disposition] = None
    # Required when the expected date moves later.
    delay_code: Optional[DelayCode] = None
    delay_note: Optional[str] = Field(default=None, max_length=500)


class PlanReview(BaseModel):
    # Required when the expected date has passed and the patient is still in (once per day).
    delay_code: Optional[DelayCode] = None
    delay_note: Optional[str] = Field(default=None, max_length=500)


ADMISSION_TYPE_CODES = tuple(lace.ADMISSION_TYPES)
COMORBIDITY_KEYS = tuple(lace.COMORBIDITIES)
AdmissionType = Literal[ADMISSION_TYPE_CODES]  # type: ignore[valid-type]
Comorbidity = Literal[COMORBIDITY_KEYS]  # type: ignore[valid-type]
SuggestedTask = Literal[risk.SUGGESTED_KEYS]  # type: ignore[valid-type]


class RiskInputs(BaseModel):
    """Readmission risk inputs (ADR-002 item 20). Fields left out stay as they are; null clears one."""
    admission_type: Optional[AdmissionType] = None
    patient_class: Optional[str] = Field(default=None, pattern=r"^[A-Z]{1,2}$")
    comorbidities: Optional[list[Comorbidity]] = Field(default=None, max_length=len(lace.COMORBIDITIES))
    # From the hospital's records, used instead of counting this system's encounters; null goes back to counting.
    ed_visits_recorded: Optional[int] = Field(default=None, ge=0, le=99)


class RiskTasks(BaseModel):
    keys: list[SuggestedTask] = Field(min_length=1, max_length=len(risk.SUGGESTED_KEYS))


class TaskBackfill(BaseModel):
    """A task done on paper during a downtime (ADR-002 item 6)."""
    completed_at: AwareDatetime
    # Required when the task is assigned to another user or role (task:override), as for PUT /tasks/{id}.
    reason: Optional[str] = Field(default=None, max_length=500)
    delay_code: Optional[DelayCode] = None
    delay_note: Optional[str] = Field(default=None, max_length=500)


class DischargeCompletion(BaseModel):
    # Only to back-enter a discharge done on paper during a downtime; omitted means now.
    completed_at: Optional[AwareDatetime] = None


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
    workflow_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("workflow:manage")),
    body: Optional[DischargeCompletion] = None,
):
    return await service.complete_discharge(db, workflow_id, user, request,
                                            completed_at=body.completed_at if body else None)


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
                                     request=request, delay_code=task_update.delay_code,
                                     delay_note=task_update.delay_note)
    return task.to_dict()


@tasks_router.post("/{task_id}/delay-reason")
async def record_task_delay(task_id: str, body: DelayIn, request: Request, db: DB, user: AuthUser):
    """Why an open task is past its due time (ADR-002 item 10), so completing it later needs no further reason.
    The assignee records it, or anyone holding task:override."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not service.is_assignee(user, task):
        await ensure_permission(db, user, "task:override")
    if task.status == "completed" or not planning.task_overdue(task):
        raise HTTPException(status_code=400, detail="Only an open task past its due time takes a delay reason")
    if await planning.task_delay_recorded(db, task_id):
        raise HTTPException(status_code=409, detail="A delay reason is already recorded for this task")
    delay = await planning.record_task_delay(db, user, task, body.code, body.note, request)
    return delay.to_dict()


@tasks_router.put("/{task_id}/backfill")
async def backfill_task(task_id: str, body: TaskBackfill, request: Request, db: DB, user: AuthUser):
    """Complete a task done on paper during a downtime, with the time it was actually done (inside a recorded
    downtime period). Same ownership rules as PUT /tasks/{id}; audited as BACKFILL_TASK_STATUS."""
    task = await service.backfill_task(db, user, task_id, completed_at=body.completed_at, reason=body.reason,
                                       request=request, delay_code=body.delay_code, delay_note=body.delay_note)
    return task.to_dict()


# ---------- discharge plans (ADR-002 items 8 and 10) ----------

@plans_router.get("/discharge-plans/codes")
async def plan_codes(user: AuthUser):
    """The coded lists the plan and template screens offer, including the readmission risk inputs."""
    return {"dispositions": list(DISPOSITIONS), "delay_codes": list(DELAY_CODES), "roles": list(ROLES),
            "admission_types": [{"code": code, "label": label, "acute": lace.is_acute(code)}
                                for code, label in lace.ADMISSION_TYPES.items()],
            "comorbidities": [{"key": key, "label": label, "weight": weight}
                              for key, (label, weight) in lace.COMORBIDITIES.items()],
            "readmission_risk": {"method": "LACE index", "version": lace.SCORE_VERSION,
                                 "threshold": lace.HIGH_RISK_THRESHOLD, "max_score": lace.MAX_SCORE,
                                 "citation": lace.CITATION}}


@plans_router.get("/discharge-plans/review")
async def plans_for_review(request: Request, db: DB, user: AuthUser, unit_id: Optional[str] = None,
                           pending_only: bool = False):
    """Active stays per unit with their plan and whether it was reviewed today (hospital time)."""
    return await planning.plans_for_review(db, user, unit_id, pending_only, request)


async def _encounter_or_404(db, encounter_id: str) -> Encounter:
    encounter = await db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return encounter


@plans_router.get("/encounters/{encounter_id}/plan")
async def get_plan(encounter_id: str, request: Request, db: DB, user: AuthUser):
    encounter = await _encounter_or_404(db, encounter_id)
    detail = await planning.plan_detail(db, encounter)
    await audit_phi_read(db, user, encounter.patient_id, f"discharge plan of encounter {encounter_id}", request)
    return detail


@plans_router.put("/encounters/{encounter_id}/plan")
async def update_plan(encounter_id: str, body: PlanUpdate, request: Request, db: DB,
                      user: CurrentUser = Depends(requires("plan:manage"))):
    """Set the expected discharge date and planned disposition. Moving the date later needs a delay reason. Counts
    as today's review."""
    changes = body.model_dump(include=set(planning.PLAN_FIELDS), exclude_unset=True)
    encounter = await planning.update_plan(db, user, encounter_id, changes, body.delay_code, body.delay_note, request)
    return await planning.plan_detail(db, encounter)


@plans_router.post("/encounters/{encounter_id}/plan/review")
async def review_plan(encounter_id: str, request: Request, db: DB, body: Optional[PlanReview] = None,
                      user: CurrentUser = Depends(requires("plan:manage"))):
    """Record today's review of the plan as it stands; a delay reason when the expected date has passed."""
    encounter = await planning.review_plan(db, user, encounter_id, body.delay_code if body else None,
                                           body.delay_note if body else None, request)
    return await planning.plan_detail(db, encounter)


@plans_router.put("/encounters/{encounter_id}/risk-inputs")
async def update_risk_inputs(encounter_id: str, body: RiskInputs, request: Request, db: DB,
                             user: CurrentUser = Depends(requires("plan:manage"))):
    """Record the readmission risk inputs (admission type, comorbidities, ED visits) and recompute the score."""
    encounter = await _encounter_or_404(db, encounter_id)
    ensure_not_erased(encounter.patient)
    await risk.update_inputs(db, user, encounter, body.model_dump(exclude_unset=True), request)
    return await planning.plan_detail(db, encounter)


@plans_router.post("/encounters/{encounter_id}/risk-tasks")
async def add_risk_tasks(encounter_id: str, body: RiskTasks, request: Request, db: DB,
                         user: CurrentUser = Depends(requires("plan:manage"))):
    """Add suggested tasks for a high readmission risk (the clinician picks which; nothing is added on its own)."""
    tasks = await planning.add_risk_tasks(db, user, encounter_id, body.keys, request)
    for workflow_id in {t.workflow_id for t in tasks if t.workflow_id}:
        await service.sync_workflow_status(db, workflow_id)  # new open tasks send pending_approval back
    return {"added": [t.to_dict() for t in tasks]}


# ---------- workflow templates (ADR-002 item 9) ----------

@templates_router.get("")
async def list_templates(db: DB, user: AuthUser):
    return await templates.list_templates(db)


@templates_router.post("")
async def create_template(body: templates.TemplateIn, request: Request, db: DB,
                          user: CurrentUser = Depends(requires("template:manage"))):
    return (await templates.create_template(db, user, body, request)).to_dict()


@templates_router.put("/{template_id}")
async def update_template(template_id: str, body: templates.TemplateIn, request: Request, db: DB,
                          user: CurrentUser = Depends(requires("template:manage"))):
    """Replace the name, tasks and active flag (the version goes up). Running workflows keep their tasks."""
    return (await templates.update_template(db, user, template_id, body, request)).to_dict()
