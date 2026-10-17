from fastapi import APIRouter
from sqlalchemy import func, select

from app.core.db import DB, utcnow
from app.core.security import AuthUser
from app.discharge.models import Task, Workflow
from app.patients.models import Patient

router = APIRouter(prefix="/analytics", tags=["Analytics"])


async def _counts_by(db, column) -> dict:
    rows = await db.execute(select(column, func.count()).group_by(column))
    return {key: count for key, count in rows if key}


@router.get("/dashboard")
async def get_dashboard_analytics(db: DB, user: AuthUser):
    patients = await _counts_by(db, Patient.status)
    workflows = await _counts_by(db, Workflow.status)
    tasks = await _counts_by(db, Task.status)

    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = await db.scalar(
        select(func.count()).select_from(Workflow).where(
            Workflow.status == "completed", Workflow.completed_at >= start_of_day
        )
    )

    # Average over the 100 most recent discharges, computed in Python to stay database-portable.
    recent = await db.execute(
        select(Workflow.started_at, Workflow.completed_at)
        .where(Workflow.status == "completed", Workflow.completed_at.is_not(None))
        .order_by(Workflow.completed_at.desc())
        .limit(100)
    )
    durations = [(done - started).total_seconds() / 3600 for started, done in recent]
    avg_hours = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "patients": {
            "total": sum(patients.values()),
            "admitted": patients.get("admitted", 0),
            "pending_discharge": patients.get("pending_discharge", 0),
            "discharged": patients.get("discharged", 0),
        },
        "workflows": {
            "active": workflows.get("in_progress", 0),
            "pending_approval": workflows.get("pending_approval", 0),
            "completed_today": completed_today,
        },
        "tasks": {"pending": tasks.get("pending", 0), "in_progress": tasks.get("in_progress", 0)},
        "metrics": {"avg_discharge_time_hours": avg_hours},
    }


@router.get("/tasks-by-role")
async def get_tasks_by_role(db: DB, user: AuthUser):
    rows = await db.execute(
        select(Task.assigned_role, func.count()).where(Task.status != "completed").group_by(Task.assigned_role)
    )
    return {role: count for role, count in rows if role}
