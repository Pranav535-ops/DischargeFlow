"""Escalation of overdue tasks (ADR-002 item 12, design change 4): clinicians do not watch a web app.

A task breaches when it is still open at its due time: the template due time (ADR-002 item 9), else its SLA target
after creation (discharge.models.due_time, TASK_SLA_HOURS by priority). Then:

- level 1, at the breach: tell the assignee (the named user, or the assigned role on the patient's unit);
- level 2, a further interval later (and never sooner than that after level 1): tell the supervising role.

Each step is one `escalations` row (unique per task and level), an in-app notification, an audit entry and, where
the policy says so, a page through the configured `Pager`. A run is idempotent: it only creates steps that are due
and missing, so running it every few minutes, twice at once, or after a failure escalates each step once. On
Postgres a per-tenant advisory lock makes a concurrent run skip instead of racing. Nothing runs on a schedule by
itself (there is no scheduler on Vercel): Vercel Cron, an integration client, an admin or the CLI calls `run_tenant`.

Notifications and pages carry no PHI: task id, category, priority, unit code, minutes overdue and a link. The task
title is free text that may name the patient, so it is never included.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analytics.service import PRIORITY_NAMES, TASK_SLA_HOURS
from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.core.tenancy import use_tenant
from app.discharge.models import due_time
from app.discharge.models import Task
from app.downtime.service import read_only_status
from app.escalations.models import Escalation
from app.escalations.pager import Page, Pager, get_pager
from app.identity.models import Tenant, Unit
from app.notifications.service import notify_role, notify_user
from app.patients.models import Patient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    level: int
    after_breach_hours: float  # 0 for level 1 (at the SLA target)
    page: bool  # also send to the external pager


# Policy defaults per priority (1 high, 2 medium, 3 low). Low priority is never paged and has no second level:
# paging for routine work trains people to ignore pages.
POLICY: dict[int, tuple[Step, ...]] = {
    1: (Step(1, 0, page=True), Step(2, 1, page=True)),
    2: (Step(1, 0, page=False), Step(2, 4, page=True)),
    3: (Step(1, 0, page=False),),
}
# Who is told at level 2, by the task's assigned role. There is no charge-nurse role yet, so nursing and pharmacy
# escalate to the physicians, and physicians' own tasks to the admins (operations).
SUPERVISOR_ROLE = {"nurse": "physician", "pharmacist": "physician", "physician": "admin", "admin": "admin"}
# Bounds per run, so one invocation stays well inside a serverless function's time limit. The rest waits for the
# next run (oldest and highest priority first).
MAX_ESCALATIONS_PER_RUN = 100
MAX_PAGES_PER_RUN = 20
MAX_PAGE_ATTEMPTS = 3
SYSTEM_ACTOR_ID = "system:escalation"


def system_actor(tenant_id: str) -> CurrentUser:
    """The audit actor of a scheduled run (Vercel Cron or the CLI), which has no user or client."""
    return CurrentUser(user_id=SYSTEM_ACTOR_ID, email="", name="Scheduled escalation run", role="system",
                       tenant_id=tenant_id, session_id=SYSTEM_ACTOR_ID, created_at=utcnow())


def policy_for(priority: Optional[int]) -> tuple[Step, ...]:
    return POLICY.get(priority or 2, POLICY[2])


def sla_hours(priority: Optional[int]) -> float:
    return TASK_SLA_HOURS.get(priority or 2, TASK_SLA_HOURS[2])


def task_link(task: Task) -> str:
    return f"/workflows/{task.workflow_id}" if task.workflow_id else "/tasks"


def _due_at(task: Task, step: Step, previous: Optional[Escalation]) -> datetime:
    breach = due_time(task.due_at, task.created_at, task.priority)
    due = breach + timedelta(hours=step.after_breach_hours)
    if previous is not None:
        # Level 2 always leaves the assignee the interval to act, even when level 1 was late (a backlog).
        due = max(due, previous.escalated_at + timedelta(hours=step.after_breach_hours))
    return due


def _overdue_minutes(due: datetime, now: datetime) -> int:
    return int((now - due).total_seconds() // 60)


async def _try_lock(db: AsyncSession, tenant_id: str) -> bool:
    if db.get_bind().dialect.name != "postgresql":
        return True  # SQLite request transactions already run one at a time (core/db.get_db)
    return bool(await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                {"k": f"dischargeflow:escalate:{tenant_id}"}))


async def _resolve_completed(db: AsyncSession, tenant_id: str, now: datetime) -> int:
    rows = (await db.execute(
        select(Escalation, Task.completed_at).join(Task, Task.task_id == Escalation.task_id)
        .where(Escalation.tenant_id == tenant_id, Escalation.resolved_at.is_(None), Task.status == "completed")
    )).all()
    for escalation, completed_at in rows:
        escalation.resolved_at = completed_at or now
        if escalation.page_status in ("pending", "failed"):
            escalation.page_status = "cancelled"
    return len(rows)


async def _notify(db: AsyncSession, task: Task, step: Step, unit_id: Optional[str], unit_code: Optional[str],
                  overdue_minutes: int) -> tuple[str, Optional[str], int]:
    """Send the in-app notification for one step; returns (role, user id or None, recipients)."""
    priority = PRIORITY_NAMES.get(task.priority, "medium")
    where = f" on unit {unit_code}" if unit_code else ""
    facts = (f"Task {task.task_id} ({task.category}, {priority} priority){where} is {overdue_minutes} min past its "
             f"{'due time' if task.due_at else f'{sla_hours(task.priority):g} h target'}.")
    link = task_link(task)
    if step.level == 1:
        if task.assigned_to:
            notify_user(db, task.assigned_to, "Overdue task", f"{facts} It is assigned to you.", "warning", link)
            return task.assigned_role, task.assigned_to, 1
        count = await notify_role(db, task.assigned_role, "Overdue task", f"{facts} It is assigned to your role.",
                                  link, unit_id=unit_id, type_="warning")
        return task.assigned_role, None, count
    role = SUPERVISOR_ROLE.get(task.assigned_role, "admin")
    count = await notify_role(db, role, "Escalated: overdue task",
                              f"{facts} The assigned {task.assigned_role} was told and it is still open.",
                              link, unit_id=unit_id, type_="error")
    return role, None, count


async def _deliver_pages(db: AsyncSession, actor: CurrentUser, pager: Pager, now: datetime) -> tuple[int, int]:
    """Send pending pages and retry failed ones (up to MAX_PAGE_ATTEMPTS). Returns (sent, failed)."""
    rows = (await db.execute(
        select(Escalation, Task.workflow_id, Task.created_at, Task.due_at, Unit.code)
        .join(Task, Task.task_id == Escalation.task_id)
        .outerjoin(Unit, Unit.unit_id == Escalation.unit_id)
        .where(Escalation.tenant_id == actor.tenant_id, Escalation.resolved_at.is_(None),
               Escalation.page_status.in_(("pending", "failed")), Escalation.page_attempts < MAX_PAGE_ATTEMPTS)
        .order_by(Escalation.priority, Escalation.escalated_at)
        .limit(MAX_PAGES_PER_RUN)
    )).all()
    sent = failed = 0
    for escalation, workflow_id, created_at, task_due_at, unit_code in rows:
        link = f"/workflows/{workflow_id}" if workflow_id else "/tasks"
        page = Page(
            escalation_id=escalation.escalation_id,
            # Stable across runs, so a gateway can drop the repeat sent after a run whose commit failed.
            idempotency_key=f"{escalation.tenant_id}:{escalation.task_id}:{escalation.level}",
            tenant_id=escalation.tenant_id, task_id=escalation.task_id, level=escalation.level,
            priority=PRIORITY_NAMES.get(escalation.priority, "medium"), category=escalation.category,
            unit=unit_code, target_role=escalation.target_role,
            overdue_minutes=_overdue_minutes(due_time(task_due_at, created_at, escalation.priority), now),
            link=f"{settings.app_base_url}{link}",
        )
        ok = await pager.send(page)
        escalation.page_attempts += 1
        if ok:
            escalation.page_status, escalation.paged_at = "sent", utcnow()
            sent += 1
        else:
            escalation.page_status = "failed"
            failed += 1
        await write_audit(db, actor, "PAGE_ESCALATION", "task", escalation.task_id,
                          f"level {escalation.level} via {pager.name}: "
                          f"{'sent' if ok else 'failed'} (attempt {escalation.page_attempts})")
    return sent, failed


async def run_tenant(db: AsyncSession, actor: CurrentUser, now: Optional[datetime] = None,
                     pager: Optional[Pager] = None) -> dict:
    """Escalate the due steps of one tenant in the caller's transaction (already bound to the tenant)."""
    now = now or utcnow()
    tenant_id = actor.tenant_id
    result: dict = {"tenant_id": tenant_id, "escalated": 0, "resolved": 0, "paged": 0, "page_failures": 0,
                    "skipped": None}
    if (await read_only_status(db, tenant_id))["read_only"]:
        # Staff can't complete tasks during a downtime, so their tasks would all breach; they are on paper anyway.
        result["skipped"] = "read_only"
        return result
    if not await _try_lock(db, tenant_id):
        result["skipped"] = "already_running"
        return result
    pager = pager or get_pager()

    result["resolved"] = await _resolve_completed(db, tenant_id, now)

    earliest = now - timedelta(hours=min(TASK_SLA_HOURS.values()))
    candidates = (await db.execute(
        select(Task, Patient.unit_id, Unit.code)
        .join(Patient, Patient.patient_id == Task.patient_id)
        .outerjoin(Unit, Unit.unit_id == Patient.unit_id)
        .where(Task.tenant_id == tenant_id, Task.status != "completed",
               or_(Task.created_at <= earliest, Task.due_at <= now))
        .order_by(Task.priority, Task.created_at)
    )).all()
    existing: dict[str, dict[int, Escalation]] = {}
    if candidates:
        for row in await db.scalars(select(Escalation).where(
                Escalation.tenant_id == tenant_id, Escalation.task_id.in_([t.task_id for t, _, _ in candidates]))):
            existing.setdefault(row.task_id, {})[row.level] = row

    for task, unit_id, unit_code in candidates:
        if result["escalated"] >= MAX_ESCALATIONS_PER_RUN:
            break
        done = existing.get(task.task_id, {})
        previous: Optional[Escalation] = None
        for step in policy_for(task.priority):
            if step.level in done:
                previous = done[step.level]
                continue
            due = _due_at(task, step, previous)
            if due > now:
                break
            overdue = _overdue_minutes(due_time(task.due_at, task.created_at, task.priority), now)
            role, user_id, recipients = await _notify(db, task, step, unit_id, unit_code, overdue)
            escalation = Escalation(
                task_id=task.task_id, level=step.level, priority=task.priority or 2, category=task.category,
                unit_id=unit_id, target_role=role, target_user_id=user_id, recipients=recipients, due_at=due,
                escalated_at=now,
                page_status=("pending" if pager.name != "none" else "disabled") if step.page else "not_required",
            )
            db.add(escalation)
            await db.flush()
            await write_audit(db, actor, "ESCALATE_TASK", "task", task.task_id,
                              f"level {step.level} to {'assignee' if user_id else role}, "
                              f"{PRIORITY_NAMES.get(task.priority, 'medium')} priority, {overdue} min overdue, "
                              f"{recipients} notified")
            result["escalated"] += 1
            break  # at most one new step per task per run

    if pager.name != "none":
        result["paged"], result["page_failures"] = await _deliver_pages(db, actor, pager, now)
    return result


async def run_all_tenants(factory: async_sessionmaker[AsyncSession], now: Optional[datetime] = None) -> dict:
    """Run every tenant, each in its own transaction, so one tenant's failure doesn't undo the others."""
    async with factory() as session:
        tenant_ids = list(await session.scalars(select(Tenant.tenant_id).order_by(Tenant.tenant_id)))
    results = []
    for tenant_id in tenant_ids:
        async with factory() as session:
            try:
                await use_tenant(session, tenant_id)
                results.append(await run_tenant(session, system_actor(tenant_id), now))
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Escalation run failed", extra={"tenant_id": tenant_id})
                results.append({"tenant_id": tenant_id, "error": True})
    return {"tenants": results}


async def active_escalations(db: AsyncSession, tenant_id: str, limit: int = 200) -> list[dict]:
    """Unresolved escalations of tasks still open, newest first, for the admin view. Ids and codes only."""
    rows = (await db.execute(
        select(Escalation, Unit.code, Task.workflow_id)
        .join(Task, Task.task_id == Escalation.task_id)
        .outerjoin(Unit, Unit.unit_id == Escalation.unit_id)
        .where(Escalation.tenant_id == tenant_id, Escalation.resolved_at.is_(None), Task.status != "completed")
        .order_by(Escalation.escalated_at.desc())
        .limit(limit)
    )).all()
    return [{**escalation.to_dict(), "unit_code": code,
             "priority_name": PRIORITY_NAMES.get(escalation.priority, "medium"),
             "link": f"/workflows/{workflow_id}" if workflow_id else "/tasks"}
            for escalation, code, workflow_id in rows]
