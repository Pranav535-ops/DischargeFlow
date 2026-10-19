"""Discharge workflow state machine: in_progress -> pending_approval -> completed.

When the hospital's ADT system discharges the patient first (HL7 A03, ADR-002 item 11): in_progress or
pending_approval -> externally_discharged -> completed, and an A13 (cancel discharge) goes back to in_progress.

A workflow discharges one encounter (stay): completing it closes the encounter (ADR-002 item 7). Its tasks come from
the template for the stay's planned disposition (item 9, discharge.templates), with prerequisites that update_task
enforces and due times; completing a task after its due time needs a delay reason (item 10, discharge.planning).
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import lifecycle
from app.ai.models import AIGeneration, DischargeSummary
from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser, ensure_permission
from app.discharge import planning, templates
from app.discharge.models import ACTIVE_WORKFLOW_STATUSES, EXTERNALLY_DISCHARGED, DelayReason, Task, Workflow, due_time
from app.downtime.service import backfill_period
from app.identity.models import User
from app.identity.permissions import PENDING_ROLE
from app.notifications.service import notify_role
from app.patients.models import Encounter, Patient
from app.patients.service import current_encounter, ensure_current_encounter

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


def visible_tasks_for(user: CurrentUser):
    """Tasks assigned to the user directly or to their role (used by the assistant's tools)."""
    return select(Task).where(or_(Task.assigned_to == user.user_id, Task.assigned_role == user.role))


async def active_workflow_for(db: AsyncSession, patient_id: str) -> Optional[Workflow]:
    return await db.scalar(
        select(Workflow).where(Workflow.patient_id == patient_id, Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES))
    )


async def encounter_for(db: AsyncSession, workflow: Workflow) -> Optional[Encounter]:
    """The stay a workflow discharges. A workflow the release before migration 0011 created has none; it is linked to
    the patient's current encounter here."""
    if workflow.encounter_id:
        return await db.get(Encounter, workflow.encounter_id)
    encounter = await current_encounter(db, workflow.patient_id)
    if encounter is not None:
        workflow.encounter_id = encounter.encounter_id
    return encounter


def task_view(task: Task, by_id: dict[str, Task], delayed: frozenset[str] = frozenset()) -> dict:
    """A task as the UI shows it: its due time (template or SLA), whether it is overdue (and whether a delay reason
    is on record), and the prerequisites it is still waiting on."""
    out = task.to_dict()
    due = due_time(task.due_at, task.created_at, task.priority)
    out["due"] = due.isoformat()
    out["overdue"] = task.status != "completed" and planning.task_overdue(task)
    out["delay_recorded"] = task.task_id in delayed
    out["blocked_by"] = [{"task_id": d, "title": by_id[d].title} for d in task.depends_on or []
                         if d in by_id and by_id[d].status != "completed"]
    return out


async def workflow_detail(db: AsyncSession, workflow: Workflow) -> dict:
    """Workflow plus its tasks (priority order), discharge summary and the stay's discharge plan, as the UI
    expects."""
    tasks = list(await db.scalars(
        select(Task).where(Task.workflow_id == workflow.workflow_id).order_by(Task.priority, Task.created_at)
    ))
    summary = await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == workflow.workflow_id))
    out = workflow.to_dict()
    by_id = {t.task_id: t for t in tasks}
    delayed = frozenset(t for t in await db.scalars(select(DelayReason.task_id).where(
        DelayReason.task_id.in_(by_id), DelayReason.kind == "task_sla_breach")) if t)
    out["tasks"] = [task_view(t, by_id, delayed) for t in tasks]
    encounter = await db.get(Encounter, workflow.encounter_id) if workflow.encounter_id else None
    out["plan"] = planning.plan_view(encounter) if encounter is not None else None
    out["discharge_summary"] = lifecycle.summary_view(summary, workflow) if summary else None
    # ADR-002 item 13: the policy deadline for a signed summary, and whether it has passed unsigned.
    deadline = lifecycle.sign_by(workflow)
    out["summary_sign_by"] = deadline.isoformat()
    out["summary_overdue"] = not (summary and summary.approved) and deadline < utcnow()
    if summary and summary.generation_id:
        gen = await db.get(AIGeneration, summary.generation_id)
        out["discharge_summary"]["generation"] = {
            k: getattr(gen, k) for k in ("provider", "model_id", "prompt_version", "grounding_warnings", "attempts")
        } if gen else None
    return out


async def open_task_count(db: AsyncSession, workflow_id: str) -> int:
    count = await db.scalar(
        select(func.count()).select_from(Task).where(Task.workflow_id == workflow_id, Task.status != "completed")
    )
    return count or 0


async def initiate_discharge(db: AsyncSession, patient_id: str, user: CurrentUser,
                             request: Optional[Request] = None) -> dict:
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if await active_workflow_for(db, patient_id):
        raise HTTPException(status_code=400, detail="Active workflow already exists")
    if patient.status == "discharged":
        raise HTTPException(status_code=400, detail="Patient is already discharged")
    encounter = await ensure_current_encounter(db, patient)
    # The template for the planned disposition (ADR-002 item 9); without one, the built-in standard checklist.
    template = await templates.template_for(db, encounter.planned_disposition)

    workflow = Workflow(
        patient_id=patient_id,
        encounter_id=encounter.encounter_id,
        initiated_by=user.user_id,
        estimated_discharge_time=utcnow() + timedelta(hours=2),
        template_id=template.template_id if template else None,
        template_version=template.version if template else None,
    )
    db.add(workflow)
    try:
        # The partial unique index catches two concurrent initiations that both passed the check above.
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=400, detail="Active workflow already exists")

    if template is not None:
        tasks = templates.tasks_from_template(template, workflow_id=workflow.workflow_id, patient_id=patient_id,
                                              expected=encounter.expected_discharge_date)
    else:
        tasks = [Task(workflow_id=workflow.workflow_id, patient_id=patient_id, **t) for t in STANDARD_TASKS]
    db.add_all(tasks)
    patient.status = "pending_discharge"

    await notify_role(db, "nurse", "New Discharge Initiated",
                      f"Discharge workflow started for patient {patient.name}", f"/patients/{patient_id}",
                      unit_id=patient.unit_id)
    await notify_role(db, "pharmacist", "Medication Review Required",
                      f"Medication review needed for {patient.name}'s discharge", f"/patients/{patient_id}",
                      unit_id=patient.unit_id)
    await db.flush()
    template_note = f" template={template.template_id} v{template.version}" if template else " template=standard"
    await write_audit(db, user, "INITIATE_DISCHARGE", "patient", patient_id,
                      f"workflow={workflow.workflow_id} tasks={len(tasks)}{template_note}", request)
    return {"workflow": workflow.to_dict(), "tasks_created": len(tasks),
            "template": {"template_id": template.template_id, "name": template.name, "version": template.version,
                         "disposition": template.disposition} if template else None}


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
    await write_audit(db, user, "CREATE_TASK", "task", task.task_id, f"patient={patient_id} role={assigned_role}")
    # A new open task sends a pending_approval workflow back to in_progress.
    await sync_workflow_status(db, task.workflow_id)
    return task


def is_assignee(user: CurrentUser, task: Task) -> bool:
    """The task is assigned to this user by name, or to their role."""
    return task.assigned_to == user.user_id or task.assigned_role == user.role


async def update_task(db: AsyncSession, user: CurrentUser, task_id: str, *, status: Optional[str] = None,
                      assigned_to: Optional[str] = None, reason: Optional[str] = None,
                      request: Optional[Request] = None,
                      backfill: Optional[tuple[datetime, str]] = None,
                      delay_code: Optional[str] = None, delay_note: Optional[str] = None) -> Task:
    """Change a task's status and/or named assignee.

    Only the assignee (the named user or anyone in the assigned role) changes the status. Someone holding
    `task:override` (physicians, admins) may change another role's task, but must give a reason, which is stored on
    the task. Assigning the task to a named user needs `task:assign`, except members of the assigned role claiming
    it for themselves. Every change is audited. `backfill` (time done, downtime id; see backfill_task) records a
    completion done on paper during a downtime at the time it was actually done.

    A task can't start or be completed while a prerequisite (`depends_on`) is not completed; nobody can override
    that. Completing a task after its due time needs a delay reason (`delay_code`, ADR-002 item 10) unless one was
    already recorded for it.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    reason = (reason or "").strip() or None

    status_changes = bool(status) and status != task.status
    override = status_changes and not is_assignee(user, task)
    if override:
        await ensure_permission(db, user, "task:override")
        if not reason:
            raise HTTPException(status_code=400, detail="A reason is required to change a task assigned to another role")
    if assigned_to and assigned_to != task.assigned_to:
        claiming = assigned_to == user.user_id and task.assigned_role == user.role
        if not claiming:
            await ensure_permission(db, user, "task:assign")
        assignee = await db.get(User, assigned_to)
        if assignee is None or assignee.role == PENDING_ROLE:
            raise HTTPException(status_code=400, detail="Assignee is not an active user")
        previous_assignee = task.assigned_to or f"role {task.assigned_role}"
        task.assigned_to = assigned_to
        await write_audit(db, user, "REASSIGN_TASK", "task", task_id, f"{previous_assignee} → {assigned_to}", request)

    if status and status_changes and status != "pending":
        await _ensure_prerequisites_done(db, task)
    if status == "completed" and status_changes:
        done_at = backfill[0] if backfill else utcnow()
        if planning.task_overdue(task, done_at) and not await planning.task_delay_recorded(db, task_id):
            if delay_code is None:
                raise HTTPException(status_code=400, detail="This task is past its due time: give a delay reason")
            await planning.record_task_delay(db, user, task, delay_code, delay_note, request)

    if status and status_changes:
        previous = task.status
        task.status = status
        if status == "completed":
            task.completed_by = user.user_id
            task.completed_at = backfill[0] if backfill else utcnow()
            task.backfilled_at = utcnow() if backfill else None
        else:
            task.completed_by = None
            task.completed_at = None
            task.backfilled_at = None
        if override:
            task.override_reason = reason
            task.override_by = user.user_id
        if backfill:
            override_note = (f"; override of assigned role {task.assigned_role}, reason stored on task"
                             if override else "")
            await write_audit(db, user, "BACKFILL_TASK_STATUS", "task", task_id,
                              f"{previous} → {status}, done at {backfill[0].isoformat()} "
                              f"(downtime {backfill[1]}{override_note})", request)
        elif override:
            # The reason stays on the task row, not in the audit entry: audit rows are immutable and survive erasure,
            # and free text may name the patient.
            await write_audit(db, user, "OVERRIDE_TASK_STATUS", "task", task_id,
                              f"{previous} → {status} (assigned role {task.assigned_role}; reason stored on task)",
                              request)
        else:
            await write_audit(db, user, "UPDATE_TASK_STATUS", "task", task_id, f"{previous} → {status}", request)
    await sync_workflow_status(db, task.workflow_id)
    await db.flush()
    await db.refresh(task)
    return task


async def _ensure_prerequisites_done(db: AsyncSession, task: Task) -> None:
    if not task.depends_on:
        return
    waiting = list(await db.scalars(select(Task.title).where(Task.task_id.in_(task.depends_on),
                                                             Task.status != "completed")))
    if waiting:
        raise HTTPException(status_code=409, detail=f"Waiting on: {', '.join(sorted(waiting))}")


async def backfill_task(db: AsyncSession, user: CurrentUser, task_id: str, *, completed_at: datetime,
                        reason: Optional[str] = None, request: Optional[Request] = None,
                        delay_code: Optional[str] = None, delay_note: Optional[str] = None) -> Task:
    """Complete a task that was done on paper during a downtime, at the time it was actually done (ADR-002 item 6).

    The same ownership rules as update_task apply (an override needs `task:override` and a reason). The time must be
    in the past, after the task was created and inside a recorded downtime period. `completed_at` keeps the real time,
    `backfilled_at` says when it was entered, and the audit entry names the downtime.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status == "completed":
        raise HTTPException(status_code=400, detail="Task is already completed")
    period = await backfill_period(db, user.tenant_id, completed_at, task.created_at)
    return await update_task(db, user, task_id, status="completed", reason=reason, request=request,
                             backfill=(completed_at, period.downtime_id), delay_code=delay_code,
                             delay_note=delay_note)


def set_workflow_status(workflow: Workflow, status: str) -> None:
    """Change status, accounting the time spent in pending_approval (read by analytics.operational_metrics)."""
    now = utcnow()
    if workflow.status == "pending_approval" and status != "pending_approval" and workflow.pending_approval_since:
        if workflow.pending_approval_seconds is not None:
            elapsed = int((now - workflow.pending_approval_since).total_seconds())
            workflow.pending_approval_seconds += elapsed
        workflow.pending_approval_since = None
    elif status == "pending_approval" and workflow.status != "pending_approval":
        workflow.pending_approval_since = now
    workflow.status = status


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
        set_workflow_status(workflow, "pending_approval")
        patient = await db.get(Patient, workflow.patient_id)
        await notify_role(db, "physician", "Discharge Ready for Approval",
                          "All tasks completed for patient. Ready for final discharge approval.",
                          f"/workflows/{workflow_id}", unit_id=patient.unit_id if patient else None)
    elif open_tasks > 0 and workflow.status == "pending_approval":
        set_workflow_status(workflow, "in_progress")


async def complete_discharge(db: AsyncSession, workflow_id: str, user: CurrentUser,
                            request: Optional[Request] = None, completed_at: Optional[datetime] = None,
                            disposition: Optional[str] = None) -> dict:
    """Discharge the patient and close the encounter. `completed_at` back-enters a discharge done on paper during a
    downtime: it must fall inside a recorded downtime period, and the audit entry names it. `disposition` is where
    the patient went (HL7 PV1-36), when the caller knows it."""
    workflow = await db.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    external = workflow.status == EXTERNALLY_DISCHARGED
    if workflow.status not in ACTIVE_WORKFLOW_STATUSES and not external:
        raise HTTPException(status_code=400, detail=f"Workflow is already {workflow.status}")
    if external and completed_at is not None:
        raise HTTPException(status_code=400, detail="The patient's departure was already recorded by the ADT feed")

    pending = await open_task_count(db, workflow_id)
    if pending > 0:
        raise HTTPException(status_code=400, detail=f"{pending} tasks still pending")

    approved = await db.scalar(
        select(DischargeSummary).where(DischargeSummary.workflow_id == workflow_id, DischargeSummary.approved.is_(True))
    )
    if not approved:
        raise HTTPException(status_code=400, detail="Discharge summary not approved")

    details = f"workflow={workflow_id}"
    if completed_at is not None:
        period = await backfill_period(db, user.tenant_id, completed_at, workflow.started_at)
        details += f" backfilled: discharged at {completed_at.isoformat()} (downtime {period.downtime_id})"
        workflow.backfilled_at = utcnow()

    set_workflow_status(workflow, "completed")
    if external:
        # The stay already ended when the patient left, and the patient may have been readmitted since: only the
        # workflow closes, at the departure time.
        workflow.completed_at = done = workflow.departed_at or utcnow()
        details += " after external discharge"
    else:
        workflow.completed_at = done = completed_at or utcnow()
        encounter = await encounter_for(db, workflow)
        if encounter is not None and encounter.status == "active":
            encounter.close(done, disposition)
            details += f" encounter={encounter.encounter_id}"
        patient = await db.get(Patient, workflow.patient_id)
        if patient:
            patient.status = "discharged"
    await write_audit(db, user, "COMPLETE_DISCHARGE", "patient", workflow.patient_id, details, request)
    return {"success": True, "completed_at": done.isoformat()}


async def record_external_discharge(db: AsyncSession, workflow: Workflow, user: CurrentUser, departed_at: datetime,
                                    disposition: Optional[str] = None, reason: str = "") -> None:
    """The hospital's ADT system discharged the patient (HL7 A03) before this workflow could complete (ADR-002 item 11).

    The patient has left, so the stay closes and the patient is discharged, matching the EHR. The workflow becomes
    externally_discharged: its open tasks stay open, due adt_documentation_hours after departure, and it completes
    through complete_discharge once they are done and the summary is approved. `reason` is why it could not complete
    (open tasks, unapproved summary); it carries no patient data.
    """
    if workflow.status not in ACTIVE_WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"Workflow is already {workflow.status}")
    set_workflow_status(workflow, EXTERNALLY_DISCHARGED)
    workflow.departed_at = departed_at
    workflow.documentation_due_at = due = departed_at + timedelta(hours=settings.adt_documentation_hours)
    details = f"workflow={workflow.workflow_id} departed={departed_at.isoformat()} due={due.isoformat()}"
    encounter = await encounter_for(db, workflow)
    if encounter is not None and encounter.status == "active":
        encounter.close(departed_at, disposition)
        details += f" encounter={encounter.encounter_id}"
    patient = await db.get(Patient, workflow.patient_id)
    if patient:
        patient.status = "discharged"
    open_tasks = await open_task_count(db, workflow.workflow_id)
    await write_audit(db, user, "EXTERNAL_DISCHARGE", "patient", workflow.patient_id,
                      f"{details} open_tasks={open_tasks}")
    message = (f"The hospital ADT system discharged {patient.name if patient else 'a patient'} before the discharge "
               f"workflow was done ({reason}). Finish the remaining tasks by {due:%Y-%m-%d %H:%M} UTC.")
    for role in ("physician", "nurse"):
        await notify_role(db, role, "Patient Left Before Discharge Was Complete", message,
                          f"/workflows/{workflow.workflow_id}", unit_id=patient.unit_id if patient else None)


async def reopen_external_discharge(db: AsyncSession, workflow: Workflow, user: CurrentUser) -> None:
    """HL7 A13 (cancel discharge) for a workflow the ADT feed discharged: back to the active state machine."""
    workflow.status = "in_progress"  # not set_workflow_status: no pending_approval time to account for here
    workflow.departed_at = None
    workflow.documentation_due_at = None
    await sync_workflow_status(db, workflow.workflow_id)
    await write_audit(db, user, "REOPEN_DISCHARGE", "patient", workflow.patient_id,
                      f"workflow={workflow.workflow_id} (ADT discharge cancelled)")
