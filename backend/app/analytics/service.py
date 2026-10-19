"""Aggregate operational metrics: counts and durations by role, never rows about individual patients.

These numbers are the only input to the AI insights feature (ai/insights.py), so nothing here may contain PHI:
no names, ids, free text or dates of individual records. Each group carries its sample size `n`, and groups smaller
than MIN_GROUP_SIZE are reported without averages so a single patient's timeline can't be read off a metric.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean, median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AIGeneration, DischargeSummary
from app.core.db import utcnow
from app.discharge.models import Task, Workflow
from app.patients.models import Encounter

MIN_GROUP_SIZE = 3
WINDOW_TASKS = 2000

# Task SLA targets in hours by priority (1 high, 2 medium, 3 low). Policy defaults for the operational metrics, not
# measurements; a hospital would set its own.
TASK_SLA_HOURS = {1: 4, 2: 12, 3: 24}
READMISSION_DAYS = 30
PRIORITY_NAMES = {1: "high", 2: "medium", 3: "low"}


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _span(start: datetime | None, end: datetime | None) -> float:
    """Hours between two timestamps the query has already filtered to be non-null."""
    if start is None or end is None:
        raise ValueError("timestamp unexpectedly NULL")
    return _hours(end - start)


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
        dwell_by_role[role].append(_span(created, done))
        dwell_by_category[category].append(_span(created, done))
    open_age_by_role: dict[str, list[float]] = defaultdict(list)
    for role, created in open_rows:
        open_age_by_role[role].append(_hours(now - created))

    durations = [_span(started, done) for started, done in await db.execute(
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


async def stay_metrics(db: AsyncSession, since: datetime) -> dict:
    """Length of stay of encounters discharged since `since`, and admissions since then that were readmissions:
    a new stay starting within READMISSION_DAYS of the same patient's previous discharge (ADR-002 item 7).

    Patient ids are only used to pair stays here; the result is counts and durations."""
    los = [_span(start, end) / 24 for start, end in await db.execute(
        select(Encounter.admitted_at, Encounter.discharged_at).where(
            Encounter.discharged_at >= since, Encounter.admitted_at.is_not(None)))]
    by_patient: dict[str, list[tuple]] = defaultdict(list)
    for patient_id, admitted, discharged in await db.execute(
            select(Encounter.patient_id, Encounter.admitted_at, Encounter.discharged_at)
            .where(Encounter.admitted_at.is_not(None)).order_by(Encounter.admitted_at)):
        by_patient[patient_id].append((admitted, discharged))
    admissions = readmissions = 0
    for stays in by_patient.values():
        for i, (admitted, _) in enumerate(stays):
            if admitted < since:
                continue
            admissions += 1
            previous_end = stays[i - 1][1] if i else None
            if previous_end is not None and admitted - previous_end <= timedelta(days=READMISSION_DAYS):
                readmissions += 1
    stats = _stats(los)
    return {
        "length_of_stay_days": {"n": stats["n"], **{k.replace("_hours", "_days"): v
                                                    for k, v in stats.items() if k != "n"}},
        "admissions": admissions,
        f"readmissions_within_{READMISSION_DAYS}_days": readmissions,
    }


def _sla_hours(priority: int | None) -> float:
    return TASK_SLA_HOURS.get(priority or 2, TASK_SLA_HOURS[2])


async def operational_metrics(db: AsyncSession, days: int = 30) -> dict:
    """Discharge throughput, time waiting for approval and task SLA breaches over the last `days` days.

    Same rules as discharge_aggregates: counts and durations only, and groups under MIN_GROUP_SIZE get no averages.
    """
    now = utcnow()
    since = now - timedelta(days=days)

    started = await db.scalar(select(func.count()).select_from(Workflow).where(Workflow.started_at >= since))
    completed = list(await db.execute(
        select(Workflow.started_at, Workflow.completed_at, Workflow.pending_approval_seconds)
        .where(Workflow.status == "completed", Workflow.completed_at >= since)))
    per_day = Counter(done.date().isoformat() for _, done, _ in completed if done)
    waits = [secs / 3600 for _, _, secs in completed if secs is not None]
    waiting_now = [_span(entered, now) for (entered,) in await db.execute(
        select(Workflow.pending_approval_since)
        .where(Workflow.status == "pending_approval", Workflow.pending_approval_since.is_not(None)))]
    waiting_count = await db.scalar(
        select(func.count()).select_from(Workflow).where(Workflow.status == "pending_approval"))

    open_breaches: dict[str, Counter] = defaultdict(Counter)
    open_total = 0
    for role, priority, created in await db.execute(
            select(Task.assigned_role, Task.priority, Task.created_at).where(Task.status != "completed")):
        open_total += 1
        if _hours(now - created) > _sla_hours(priority):
            open_breaches[role][PRIORITY_NAMES.get(priority, "medium")] += 1
    done_by_role: dict[str, list[bool]] = defaultdict(list)
    for role, priority, created, done in await db.execute(
            select(Task.assigned_role, Task.priority, Task.created_at, Task.completed_at)
            .where(Task.status == "completed", Task.completed_at >= since).limit(WINDOW_TASKS)):
        done_by_role[role].append(_span(created, done) > _sla_hours(priority))

    def _rate(flags: list[bool]) -> dict:
        out: dict = {"n": len(flags), "breached": sum(flags)}
        if len(flags) >= MIN_GROUP_SIZE:
            out["breach_rate"] = round(sum(flags) / len(flags), 3)
        return out

    all_done = [f for flags in done_by_role.values() for f in flags]
    return {
        "window_days": days,
        "throughput": {
            "discharges_started": started,
            "discharges_completed": len(completed),
            "completed_per_day": dict(sorted(per_day.items())),
            "discharge_duration": _stats([_span(start, done) for start, done, _ in completed]),
        },
        "pending_approval": {
            # Completed discharges whose approval wait was tracked (those started before migration 0006 were not).
            "wait": _stats(waits),
            "untracked_completed": len(completed) - len(waits),
            "waiting_now": waiting_count,
            "oldest_waiting_hours": round(max(waiting_now), 1) if len(waiting_now) >= MIN_GROUP_SIZE else None,
        },
        "task_sla": {
            "targets_hours": {PRIORITY_NAMES[p]: h for p, h in TASK_SLA_HOURS.items()},
            "open_tasks": open_total,
            "open_breaching_by_role": {r: dict(sorted(c.items())) for r, c in sorted(open_breaches.items())},
            "completed": _rate(all_done),
            "completed_by_role": {r: _rate(v) for r, v in sorted(done_by_role.items())},
        },
        "stays": await stay_metrics(db, since),
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
