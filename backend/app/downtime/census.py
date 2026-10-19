"""Discharge census (ADR-002 item 6): every patient still in hospital, with discharge status and open tasks.

Location and admission come from the patient's current encounter (ADR-002 item 7), with the patient's mirrored
fields as the fallback for a patient the previous release created without one.

Printed at the start of a downtime (and on a schedule before one) so each unit can carry on from paper. It reads
through the request's transaction, so unit scope (RLS) limits it like every other patient list.
"""
import csv
import io
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import DischargeSummary
from app.core.db import utcnow
from app.discharge.models import ACTIVE_WORKFLOW_STATUSES, Task, Workflow
from app.identity.models import Unit, User
from app.patients.models import Encounter, Patient

UNASSIGNED = "unassigned"
PRIORITY = {1: "high", 2: "medium", 3: "low"}
CSV_COLUMNS = (
    "unit", "room", "patient", "mrn", "visit_number", "date_of_birth", "admission_date", "attending_physician", "patient_status",
    "discharge_status", "estimated_discharge", "summary_approved", "open_tasks", "open_task_list",
)


async def build_census(db: AsyncSession, unit_id: Optional[str] = None) -> dict:
    """Patients not yet discharged, by unit and room. `unit_id` limits it to one unit ("unassigned": no unit)."""
    stmt = select(Patient).where(Patient.status != "discharged", Patient.gdpr_erased.is_(False))
    if unit_id == UNASSIGNED:
        stmt = stmt.where(Patient.unit_id.is_(None))
    elif unit_id:
        stmt = stmt.where(Patient.unit_id == unit_id)
    patients = list(await db.scalars(stmt))
    ids = [p.patient_id for p in patients]

    units = {u.unit_id: u for u in await db.scalars(select(Unit))}
    encounters = {e.patient_id: e for e in await db.scalars(
        select(Encounter).where(Encounter.patient_id.in_(ids), Encounter.status == "active"))}
    workflows = {w.patient_id: w for w in await db.scalars(
        select(Workflow).where(Workflow.patient_id.in_(ids), Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES)))}
    approved = set(await db.scalars(select(DischargeSummary.workflow_id).where(
        DischargeSummary.workflow_id.in_([w.workflow_id for w in workflows.values()]),
        DischargeSummary.approved.is_(True))))
    tasks: dict[str, list[Task]] = {}
    for task in await db.scalars(select(Task).where(Task.patient_id.in_(ids), Task.status != "completed")
                                 .order_by(Task.priority, Task.created_at)):
        tasks.setdefault(task.patient_id, []).append(task)
    assignee_ids = {t.assigned_to for ts in tasks.values() for t in ts if t.assigned_to}
    names = dict((await db.execute(select(User.user_id, User.name).where(User.user_id.in_(assignee_ids)))).all())

    rows = []
    for p in patients:
        unit = units.get(p.unit_id) if p.unit_id else None
        workflow = workflows.get(p.patient_id)
        encounter = encounters.get(p.patient_id)
        rows.append({
            "patient_id": p.patient_id,
            "unit_id": p.unit_id,
            "unit_code": unit.code if unit else None,
            "unit_name": unit.name if unit else None,
            "encounter_id": encounter.encounter_id if encounter else None,
            "visit_number": encounter.visit_number if encounter else None,
            "room_number": (encounter.room_number if encounter else None) or p.room_number,
            "name": p.name,
            "mrn": p.mrn,
            "date_of_birth": p.date_of_birth,
            "admission_date": (encounter.admitted_at.date().isoformat()
                               if encounter and encounter.admitted_at else p.admission_date),
            "attending_physician": p.attending_physician,
            "status": p.status,
            "workflow_id": workflow.workflow_id if workflow else None,
            "discharge_status": workflow.status if workflow else "not_started",
            "estimated_discharge_time": (workflow.estimated_discharge_time.isoformat()
                                         if workflow and workflow.estimated_discharge_time else None),
            "summary_approved": bool(workflow and workflow.workflow_id in approved),
            "open_tasks": [{
                "task_id": t.task_id,
                "title": t.title,
                "assigned_role": t.assigned_role,
                "assigned_to": t.assigned_to,
                "assigned_to_name": names.get(t.assigned_to) if t.assigned_to else None,
                "priority": t.priority,
                "status": t.status,
            } for t in tasks.get(p.patient_id, [])],
        })
    # Encrypted names can't be sorted in SQL; unit and room are enough for a ward round anyway.
    rows.sort(key=lambda r: (r["unit_code"] is None, r["unit_code"] or "", r["room_number"] or "", r["name"] or ""))
    selected = units.get(unit_id) if unit_id and unit_id != UNASSIGNED else None
    return {
        "generated_at": utcnow().isoformat(),
        "unit": selected.to_dict() if selected else ({"unit_id": UNASSIGNED} if unit_id == UNASSIGNED else None),
        "patients": rows,
        "totals": {
            "patients": len(rows),
            "in_discharge": sum(r["workflow_id"] is not None for r in rows),
            "pending_approval": sum(r["discharge_status"] == "pending_approval" for r in rows),
            "open_tasks": sum(len(r["open_tasks"]) for r in rows),
        },
    }


def _cell(value) -> str:
    """Neutralise spreadsheet formulas (CSV injection): a cell starting with = + - @ is read as text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def census_csv(census: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in census["patients"]:
        task_list = "; ".join(
            f"{t['title']} ({t['assigned_to_name'] or t['assigned_role']}, {PRIORITY.get(t['priority'], t['priority'])}"
            f"{', in progress' if t['status'] == 'in_progress' else ''})"
            for t in r["open_tasks"]
        )
        writer.writerow([_cell(v) for v in (
            r["unit_code"] or "", r["room_number"], r["name"], r["mrn"], r["visit_number"], r["date_of_birth"],
            r["admission_date"],
            r["attending_physician"], r["status"], r["discharge_status"], r["estimated_discharge_time"],
            "yes" if r["summary_approved"] else "no", len(r["open_tasks"]), task_list,
        )])
    return out.getvalue()
