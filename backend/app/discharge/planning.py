"""Discharge plan from admission and delay reasons (ADR-002 items 8 and 10).

The plan lives on the encounter: an expected discharge date and a planned disposition (which picks the workflow
template, item 9). It is reviewed daily in rounds: `plans_for_review` lists the active stays per unit with whether
their plan was reviewed today (hospital time), and `review_plan` records who reviewed it and when. Changing the plan
counts as a review.

A delay reason (a code from DELAY_CODES) is required when the expected date slips: moved later, or reviewed after it
has passed while the patient is still in. One is also required to complete a task after its due time (see
discharge.service.update_task), or can be given beforehand with `record_task_delay`. Audit entries carry field names,
codes and ids, never dates or the free-text note.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit_phi_read, write_audit
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import (ACTIVE_WORKFLOW_STATUSES, DELAY_CODES, DISPOSITIONS, DelayReason, Task, Workflow,
                                  due_time)
from app.discharge.templates import due_at_for, hospital_today, hospital_zone
from app.identity.models import Unit, User
from app.patients.models import REDACTED, Encounter
from app.risk import lace
from app.risk import service as risk

PLAN_FIELDS = ("expected_discharge_date", "planned_disposition")


def check_delay(code: Optional[str], note: Optional[str]) -> tuple[str, Optional[str]]:
    note = (note or "").strip() or None
    if code not in DELAY_CODES:
        raise HTTPException(status_code=400, detail=f"A delay reason is required: one of {', '.join(DELAY_CODES)}")
    if code == "other" and not note:
        raise HTTPException(status_code=400, detail="Describe the delay when the reason is 'other'")
    return code, note


async def _active_encounter(db: AsyncSession, encounter_id: str) -> Encounter:
    encounter = await db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    if encounter.status != "active":
        raise HTTPException(status_code=400, detail="The patient has already been discharged from this stay")
    return encounter


async def _record_slip(db: AsyncSession, user: CurrentUser, encounter: Encounter, code: str, note: Optional[str],
                       slip_days: int) -> DelayReason:
    delay = DelayReason(patient_id=encounter.patient_id, encounter_id=encounter.encounter_id,
                        kind="expected_date_slip", code=code, note=note, slip_days=slip_days,
                        recorded_by=user.user_id)
    db.add(delay)
    await db.flush()
    return delay


async def _move_due_times(db: AsyncSession, encounter: Encounter) -> None:
    """Template tasks of the stay's active workflow are due relative to the expected date: move them with it."""
    workflow = await db.scalar(select(Workflow).where(Workflow.encounter_id == encounter.encounter_id,
                                                      Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES)))
    if workflow is None:
        return
    now = utcnow()
    for task in await db.scalars(select(Task).where(Task.workflow_id == workflow.workflow_id,
                                                    Task.status != "completed",
                                                    Task.due_offset_hours.is_not(None))):
        task.due_at = due_at_for(encounter.expected_discharge_date, task.due_offset_hours, task.priority, now)


async def update_plan(db: AsyncSession, user: CurrentUser, encounter_id: str, changes: dict,
                      delay_code: Optional[str] = None, delay_note: Optional[str] = None,
                      request: Optional[Request] = None) -> Encounter:
    """Set the expected discharge date and/or planned disposition (keys of `changes`; None clears one).

    Moving the date later needs a delay reason. A disposition changed after the discharge started does not change
    the workflow's tasks (they came from the template when it started)."""
    encounter = await _active_encounter(db, encounter_id)
    disposition = changes.get("planned_disposition")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise HTTPException(status_code=400, detail=f"planned_disposition must be one of {', '.join(DISPOSITIONS)}")
    changed = sorted(f for f in PLAN_FIELDS if f in changes and changes[f] != getattr(encounter, f))

    details = f"encounter={encounter_id} fields: {', '.join(changed) or 'none'}"
    old: Optional[date] = encounter.expected_discharge_date
    new: Optional[date] = changes.get("expected_discharge_date", old)
    if "expected_discharge_date" in changed and old is not None and new is not None and new > old:
        code, note = check_delay(delay_code, delay_note)
        delay = await _record_slip(db, user, encounter, code, note, (new - old).days)
        details += f" slip: delay={delay.delay_id} code={code}"
    for field in changed:
        setattr(encounter, field, changes[field])
    if encounter.first_expected_discharge_date is None and encounter.expected_discharge_date is not None:
        encounter.first_expected_discharge_date = encounter.expected_discharge_date
    if "expected_discharge_date" in changed:
        await _move_due_times(db, encounter)
        encounter.rescore()  # the expected length of stay is the readmission score's L (ADR-002 item 20)
    encounter.plan_reviewed_at = utcnow()
    encounter.plan_reviewed_by = user.user_id
    await db.flush()
    await write_audit(db, user, "UPDATE_DISCHARGE_PLAN", "patient", encounter.patient_id, details, request)
    return encounter


async def review_plan(db: AsyncSession, user: CurrentUser, encounter_id: str, delay_code: Optional[str] = None,
                      delay_note: Optional[str] = None, request: Optional[Request] = None) -> Encounter:
    """The daily review: the plan stands as it is. When the expected date has passed and the patient is still in,
    the review records why (a delay reason, once per day)."""
    encounter = await _active_encounter(db, encounter_id)
    today = hospital_today()
    details = f"encounter={encounter_id}"
    expected = encounter.expected_discharge_date
    if expected is not None and expected < today and not _reviewed_on(encounter, today):
        code, note = check_delay(delay_code, delay_note)
        delay = await _record_slip(db, user, encounter, code, note, (today - expected).days)
        details += f" expected date passed: delay={delay.delay_id} code={code}"
    encounter.plan_reviewed_at = utcnow()
    encounter.plan_reviewed_by = user.user_id
    await db.flush()
    await write_audit(db, user, "REVIEW_DISCHARGE_PLAN", "patient", encounter.patient_id, details, request)
    return encounter


def _reviewed_on(encounter: Encounter, day: date) -> bool:
    reviewed = encounter.plan_reviewed_at
    return reviewed is not None and reviewed.astimezone(hospital_zone()).date() == day


def plan_view(encounter: Encounter, reviewer_name: Optional[str] = None) -> dict:
    today = hospital_today()
    expected = encounter.expected_discharge_date
    return {
        "encounter_id": encounter.encounter_id,
        "patient_id": encounter.patient_id,
        "expected_discharge_date": expected.isoformat() if expected else None,
        "planned_disposition": encounter.planned_disposition,
        "plan_reviewed_at": encounter.plan_reviewed_at.isoformat() if encounter.plan_reviewed_at else None,
        "plan_reviewed_by": encounter.plan_reviewed_by,
        "plan_reviewed_by_name": reviewer_name,
        "reviewed_today": _reviewed_on(encounter, today),
        "expected_date_passed": expected is not None and expected < today,
        "today": today.isoformat(),
    }


async def plan_detail(db: AsyncSession, encounter: Encounter) -> dict:
    await risk.ensure_current(db, encounter)
    reviewer = await db.get(User, encounter.plan_reviewed_by) if encounter.plan_reviewed_by else None
    delays = await db.scalars(select(DelayReason).where(DelayReason.encounter_id == encounter.encounter_id)
                              .order_by(DelayReason.created_at.desc()))
    readmission_risk = risk.view(encounter)
    if readmission_risk is not None:
        readmission_risk["suggested_tasks"] = await _suggestions(db, encounter, readmission_risk["high_risk"])
    return {**plan_view(encounter, reviewer.name if reviewer else None),
            "delays": [d.to_dict() for d in delays], "readmission_risk": readmission_risk}


# ---------- readmission risk (ADR-002 item 20) ----------

async def _risk_task_titles(db: AsyncSession, encounter: Encounter) -> set[str]:
    """Titles of the suggested tasks this stay already has (in any workflow of the stay, or ad hoc since admission)."""
    titles = {t["title"] for t in risk.SUGGESTED_TASKS}
    workflow_ids = select(Workflow.workflow_id).where(Workflow.encounter_id == encounter.encounter_id)
    stmt = select(Task.title).where(Task.patient_id == encounter.patient_id, Task.title.in_(titles),
                                    Task.workflow_id.in_(workflow_ids) | (
                                        Task.workflow_id.is_(None) & (Task.created_at >= encounter.created_at)))
    return set(await db.scalars(stmt))


async def _suggestions(db: AsyncSession, encounter: Encounter, high_risk: bool) -> list[dict]:
    """The extra tasks offered for a high-risk stay (never added on their own), each marked if already added."""
    if not high_risk or encounter.status != "active":
        return []
    have = await _risk_task_titles(db, encounter)
    return [{**{k: t[k] for k in ("key", "title", "description", "category", "assigned_role", "priority")},
             "added": t["title"] in have} for t in risk.SUGGESTED_TASKS]


async def add_risk_tasks(db: AsyncSession, user: CurrentUser, encounter_id: str, keys: list[str],
                         request: Optional[Request] = None) -> list[Task]:
    """Add the chosen suggested tasks to the stay: to its discharge workflow if one is running, otherwise as ad hoc
    tasks. Only for a high-risk score, and each task once. Due times follow the expected date, as template tasks."""
    encounter = await _active_encounter(db, encounter_id)
    await risk.ensure_current(db, encounter)
    if encounter.readmission_score is None or not lace.is_high_risk(encounter.readmission_score):
        raise HTTPException(status_code=409, detail="Extra tasks are suggested only for a high readmission risk")
    unknown = sorted(set(keys) - set(risk.SUGGESTED_KEYS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown suggested tasks: {', '.join(unknown)}")
    have = await _risk_task_titles(db, encounter)
    workflow = await db.scalar(select(Workflow).where(Workflow.encounter_id == encounter.encounter_id,
                                                      Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES)))
    now = utcnow()
    added = []
    for spec in risk.SUGGESTED_TASKS:
        if spec["key"] not in keys or spec["title"] in have:
            continue
        task = Task(patient_id=encounter.patient_id, workflow_id=workflow.workflow_id if workflow else None,
                    title=spec["title"], description=spec["description"], category=spec["category"],
                    assigned_role=spec["assigned_role"], priority=spec["priority"], created_by=user.user_id,
                    due_offset_hours=spec["due_offset_hours"],
                    due_at=due_at_for(encounter.expected_discharge_date, spec["due_offset_hours"], spec["priority"],
                                      now))
        db.add(task)
        added.append(task)
    if not added:
        return []
    await db.flush()
    for task in added:
        await write_audit(db, user, "CREATE_TASK", "task", task.task_id,
                          f"patient={task.patient_id} role={task.assigned_role} source=readmission_risk", request)
    await write_audit(db, user, "ADD_RISK_TASKS", "patient", encounter.patient_id,
                      f"encounter={encounter_id} tasks: {', '.join(t.task_id for t in added)}", request)
    return added


async def plans_for_review(db: AsyncSession, user: CurrentUser, unit_id: Optional[str] = None,
                           pending_only: bool = False, request: Optional[Request] = None) -> dict:
    """Active stays grouped by unit, each with its plan and whether it was reviewed today. Unit scope (RLS) limits
    what a scoped user sees. `unit_id` filters to one unit ("unassigned" for none). One PHI_READ entry per request."""
    stmt = select(Encounter).where(Encounter.status == "active")
    if unit_id == "unassigned":
        stmt = stmt.where(Encounter.unit_id.is_(None))
    elif unit_id:
        stmt = stmt.where(Encounter.unit_id == unit_id)
    encounters = list(await db.scalars(stmt))
    units = {u.unit_id: u for u in await db.scalars(select(Unit))}
    reviewers = {u.user_id: u.name for u in await db.scalars(
        select(User).where(User.user_id.in_({e.plan_reviewed_by for e in encounters if e.plan_reviewed_by})))}
    workflows = {w.encounter_id: w.status for w in await db.scalars(
        select(Workflow).where(Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES)))}

    groups: dict[Optional[str], dict] = {}
    for encounter in encounters:
        await risk.ensure_current(db, encounter)
        view = plan_view(encounter, reviewers.get(encounter.plan_reviewed_by or ""))
        view["readmission_risk"] = risk.summary(encounter)  # the list carries the flag; the plan explains it
        if pending_only and view["reviewed_today"]:
            continue
        patient = encounter.patient
        view.update(patient_name=patient.name if not patient.gdpr_erased else REDACTED,
                    room_number=encounter.room_number, admitted_at=(
                        encounter.admitted_at.isoformat() if encounter.admitted_at else None),
                    workflow_status=workflows.get(encounter.encounter_id))
        unit = units.get(encounter.unit_id or "")
        group = groups.setdefault(encounter.unit_id, {
            "unit_id": encounter.unit_id, "unit_code": unit.code if unit else None,
            "unit_name": unit.name if unit else "Unassigned", "plans": []})
        group["plans"].append(view)
    out = []
    for group in sorted(groups.values(), key=lambda g: (g["unit_id"] is None, g["unit_code"] or "")):
        # Not reviewed today first, then by expected date (none last).
        group["plans"].sort(key=lambda p: (p["reviewed_today"], p["expected_discharge_date"] or "9999",
                                           p["room_number"] or ""))
        group["total"] = len(group["plans"])
        group["not_reviewed_today"] = sum(not p["reviewed_today"] for p in group["plans"])
        out.append(group)
    count = sum(g["total"] for g in out)
    await audit_phi_read(db, user, None, f"discharge plan review list ({count} records)", request)
    return {"today": hospital_today().isoformat(), "units": out}


# ---------- tasks past their due time ----------

def task_overdue(task: Task, at: Optional[datetime] = None) -> bool:
    when = at or utcnow()
    return when > due_time(task.due_at, task.created_at, task.priority)


async def task_delay_recorded(db: AsyncSession, task_id: str) -> bool:
    return bool(await db.scalar(select(DelayReason.delay_id).where(DelayReason.task_id == task_id,
                                                                   DelayReason.kind == "task_sla_breach")))


async def record_task_delay(db: AsyncSession, user: CurrentUser, task: Task, code: Optional[str],
                            note: Optional[str], request: Optional[Request] = None) -> DelayReason:
    """Why a task is past its due time. Called by update_task when a late task is completed, or directly
    (POST /tasks/{id}/delay-reason) while it is still open."""
    code, note = check_delay(code, note)
    workflow = await db.get(Workflow, task.workflow_id) if task.workflow_id else None
    delay = DelayReason(patient_id=task.patient_id, task_id=task.task_id,
                        encounter_id=workflow.encounter_id if workflow else None, kind="task_sla_breach", code=code,
                        note=note, recorded_by=user.user_id)
    db.add(delay)
    await db.flush()
    await write_audit(db, user, "RECORD_DELAY_REASON", "task", task.task_id,
                      f"delay={delay.delay_id} kind=task_sla_breach code={code}", request)
    return delay


async def redact_delay_notes(db: AsyncSession, patient_id: str) -> None:
    """Erasure: notes are free text and may name the patient."""
    for delay in await db.scalars(select(DelayReason).where(DelayReason.patient_id == patient_id,
                                                            DelayReason.note.is_not(None))):
        delay.note = REDACTED
