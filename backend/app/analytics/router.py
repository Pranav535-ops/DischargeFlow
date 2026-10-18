from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.ai import insights as ai_insights
from app.ai.llm import get_llm_client
from app.ai.models import AIGeneration
from app.analytics import service as analytics_service
from app.audit.service import write_audit
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, requires
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
    durations = [(done - started).total_seconds() / 3600 for started, done in recent if started and done]
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


@router.get("/aggregates")
async def get_aggregates(db: DB, user: CurrentUser = Depends(requires("insights:read"))):
    """The aggregate metrics the AI insights are based on (no patient-level data)."""
    return await analytics_service.discharge_aggregates(db)


@router.get("/operations")
async def get_operational_metrics(db: DB, days: int = Query(30, ge=1, le=365),
                                  user: CurrentUser = Depends(requires("insights:read"))):
    """Throughput, approval wait and task SLA breaches (aggregate only, same rules as /aggregates)."""
    return await analytics_service.operational_metrics(db, days)


@router.post("/insights")
async def generate_insights(request: Request, db: DB, user: CurrentUser = Depends(requires("insights:read"))):
    """Narrative insights from aggregates only. Returns the numbers even when AI is unavailable."""
    aggregates = await analytics_service.discharge_aggregates(db)
    try:
        client = get_llm_client("insights")
    except HTTPException as e:
        return {"aggregates": aggregates, "insights": None, "unavailable": e.detail}

    prov, doc, rejected = await ai_insights.generate_insights(client, aggregates)
    generation = AIGeneration(purpose="insights", user_id=user.user_id, **prov)
    db.add(generation)
    await db.flush()
    await write_audit(db, user, "AI_INSIGHTS_GENERATED", "analytics", generation.generation_id,
                      f"{prov['prompt_version']} {prov['model_id']} status={prov['status']} rejected={len(rejected)}",
                      request)
    return {
        "aggregates": aggregates,
        "insights": doc.model_dump() if doc else None,
        "rejected_citations": rejected,
        "generation": {k: getattr(generation, k) for k in
                       ("generation_id", "provider", "model_id", "prompt_version", "status", "latency_ms")},
        "unavailable": None if doc else "Insight generation failed; the metrics above are still current.",
    }
