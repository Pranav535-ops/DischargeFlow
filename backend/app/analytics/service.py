"""Aggregate operational metrics: counts and durations by role, never rows about individual patients.

These numbers are the only input to the AI insights feature (ai/insights.py), so nothing here may contain PHI:
no names, ids, free text or dates of individual records. Each group carries its sample size `n`, and groups smaller
than MIN_GROUP_SIZE are reported without averages so a single patient's timeline can't be read off a metric.
"""
from collections import defaultdict
from statistics import mean, median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AIGeneration, DischargeSummary
from app.core.db import utcnow
from app.discharge.models import Task, Workflow

MIN_GROUP_SIZE = 3
WINDOW_TASKS = 2000


def _hours(delta) -> float:
    return delta.total_seconds() / 3600


def _stats(values: list[float]) -> dict:
    if len(values) < MIN_GROUP_SIZE:
        return {"n": len(values)}
    return {"n": len(values), "avg_hours": round(mean(values), 1), "median_hours": round(median(values), 1)}


async def discharge_aggregates(db: AsyncSession) -> dict:
    now = utcnow()
    completed = list(await db.execute(
        select(Task.assigned_role, Task.category, Task.created_at, Task.completed_at)
        .where(Task.status == "completed", Task.completed_at.is_not(None))
        .order_by(Task.completed_at.desc()).limit(WINDOW_TASKS)))
    open_rows = list(await db.execute(
        select(Task.assigned_role, Task.created_at).where(Task.status != "completed")))

    dwell_by_role: dict[str, list[float]] = defaultdict(list)
    dwell_by_category: dict[str, list[float]] = defaultdict(list)
    for role, category, created, done in completed:
        dwell_by_role[role].append(_hours(done - created))
        dwell_by_category[category].append(_hours(done - created))
    open_age_by_role: dict[str, list[float]] = defaultdict(list)
    for role, created in open_rows:
        open_age_by_role[role].append(_hours(now - created))

    durations = [_hours(done - started) for started, done in await db.execute(
        select(Workflow.started_at, Workflow.completed_at)
        .where(Workflow.status == "completed", Workflow.completed_at.is_not(None))
        .order_by(Workflow.completed_at.desc()).limit(500))]
    workflow_status = dict((await db.execute(select(Workflow.status, func.count()).group_by(Workflow.status))).all())

    summaries = (await db.execute(select(
        func.count(DischargeSummary.summary_id),
        func.count(DischargeSummary.approved_at),
        func.avg(DischargeSummary.edit_similarity),
    ).where(DischargeSummary.generation_id.is_not(None)))).one()
    failed_generations = await db.scalar(select(func.count()).select_from(AIGeneration).where(
        AIGeneration.purpose == "discharge_summary", AIGeneration.status != "succeeded"))

    return {
        "task_completion_time_by_role": {r: _stats(v) for r, v in sorted(dwell_by_role.items())},
        "task_completion_time_by_category": {c: _stats(v) for c, v in sorted(dwell_by_category.items())},
        "open_tasks_by_role": {r: {"n": len(v), "oldest_hours": round(max(v), 1) if len(v) >= MIN_GROUP_SIZE else None}
                               for r, v in sorted(open_age_by_role.items())},
        "discharge_duration": _stats(durations),
        "workflows_by_status": {k: v for k, v in sorted(workflow_status.items()) if k},
        "ai_summaries": {
            "generated": summaries[0],
            "approved": summaries[1],
            "avg_edit_similarity": round(float(summaries[2]), 3) if summaries[2] is not None else None,
            "failed_generations": failed_generations,
        },
    }


def metric_paths(aggregates: dict, prefix: str = "") -> set[str]:
    """Every metric path in the aggregates, e.g. 'task_completion_time_by_role.pharmacist.avg_hours'."""
    paths: set[str] = set()
    for key, value in aggregates.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            paths |= metric_paths(value, f"{path}.")
        else:
            paths.add(path)
    return paths
