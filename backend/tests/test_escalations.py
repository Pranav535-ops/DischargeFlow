"""Escalation of overdue tasks (ADR-002 item 12): policy, idempotency, pages without PHI, run authentication."""
import dataclasses
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.audit.models import AuditLog
from app.core.db import get_session_factory, utcnow
from app.core.tenancy import current_tenant_id, use_tenant
from app.discharge.models import Task
from app.downtime.models import DowntimePeriod
from app.escalations import router as escalations_router
from app.escalations.models import Escalation
from app.escalations.pager import SIGNATURE_HEADER, WebhookPager, sign
from app.escalations.service import MAX_PAGE_ATTEMPTS, run_all_tenants, run_tenant, system_actor
from app.main import app
from app.notifications.models import Notification
from tests.conftest import DEFAULT_TENANT, requires_postgres

pytestmark = pytest.mark.anyio

TITLE = "Call Jane Doe's daughter about the walker"
SECRET = "w" * 40
CRON = "c" * 40


async def _task(db, patient_id, *, hours_old: float, priority: int = 1, role: str = "nurse", assigned_to=None,
                workflow_id=None) -> str:
    task = Task(patient_id=patient_id, title=TITLE, assigned_role=role, assigned_to=assigned_to, priority=priority,
                category="education", workflow_id=workflow_id, created_at=utcnow() - timedelta(hours=hours_old))
    db.add(task)
    await db.commit()
    return task.task_id


async def _run(session_factory, now=None, pager=None, tenant_id=DEFAULT_TENANT) -> dict:
    token = current_tenant_id.set(tenant_id)
    try:
        async with session_factory() as session:
            await use_tenant(session, tenant_id)
            result = await run_tenant(session, system_actor(tenant_id), now, pager)
            await session.commit()
        return result
    finally:
        current_tenant_id.reset(token)


async def _escalations(db, task_id) -> list[Escalation]:
    db.expire_all()
    return list(await db.scalars(select(Escalation).where(Escalation.task_id == task_id).order_by(Escalation.level)))


class FakeGateway:
    """An httpx transport standing in for the hospital's paging gateway."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def pager(self) -> WebhookPager:
        return WebhookPager("https://pager.example.test/hook", SECRET, transport=httpx.MockTransport(self.handler))


async def test_high_priority_task_escalates_to_role_then_supervisor_once_each(
        db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    nurse, _ = await make_user("nurse")
    physician, _ = await make_user("physician")
    patient = await create_patient(admin)
    task_id = await _task(db, patient["patient_id"], hours_old=4.5)

    first = await _run(session_factory)
    assert first["escalated"] == 1 and first["skipped"] is None
    assert (await _run(session_factory))["escalated"] == 0  # idempotent
    [level1] = await _escalations(db, task_id)
    assert (level1.level, level1.target_role, level1.target_user_id, level1.recipients) == (1, "nurse", None, 1)
    assert level1.page_status == "disabled"  # policy pages high priority, but no pager is configured
    notes = list(await db.scalars(select(Notification).where(Notification.user_id == nurse["user_id"])))
    assert len(notes) == 1 and notes[0].type == "warning" and task_id in notes[0].message

    # Level 2 is due one hour after the breach, but never sooner than an hour after level 1.
    assert (await _run(session_factory, now=utcnow() + timedelta(minutes=50)))["escalated"] == 0
    assert (await _run(session_factory, now=utcnow() + timedelta(minutes=61)))["escalated"] == 1
    levels = await _escalations(db, task_id)
    assert [(e.level, e.target_role) for e in levels] == [(1, "nurse"), (2, "physician")]
    told = list(await db.scalars(select(Notification).where(Notification.user_id == physician["user_id"])))
    assert len(told) == 1 and told[0].type == "error"
    assert (await _run(session_factory, now=utcnow() + timedelta(hours=5)))["escalated"] == 0  # no level 3

    actions = [a for (a,) in await db.execute(
        select(AuditLog.action).where(AuditLog.resource_id == task_id, AuditLog.action == "ESCALATE_TASK"))]
    assert len(actions) == 2

    task = await db.get(Task, task_id)
    task.status, task.completed_at = "completed", utcnow()
    await db.commit()
    assert (await _run(session_factory))["resolved"] == 2
    assert all(e.resolved_at is not None for e in await _escalations(db, task_id))


async def test_policy_by_priority_and_named_assignee(db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    owner, _ = await make_user("nurse")
    other, _ = await make_user("nurse")
    pid = (await create_patient(admin))["patient_id"]
    fresh = await _task(db, pid, hours_old=3, priority=1)  # under its 4 h target
    medium = await _task(db, pid, hours_old=13, priority=2, assigned_to=owner["user_id"])
    low = await _task(db, pid, hours_old=30, priority=3)

    assert (await _run(session_factory))["escalated"] == 2
    assert await _escalations(db, fresh) == []
    [m1] = await _escalations(db, medium)
    assert (m1.target_user_id, m1.recipients, m1.page_status) == (owner["user_id"], 1, "not_required")
    assert not list(await db.scalars(select(Notification).where(Notification.user_id == other["user_id"],
                                                                Notification.message.contains(medium))))
    later = utcnow() + timedelta(days=2)
    await _run(session_factory, now=later)
    assert [e.level for e in await _escalations(db, low)] == [1]  # low priority has no second level
    assert [e.page_status for e in await _escalations(db, medium)] == ["not_required", "disabled"]


async def test_pages_are_signed_and_carry_no_phi(db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    await make_user("nurse")
    patient = await create_patient(admin, name="Jane Doe", diagnosis="Community-acquired pneumonia")
    task_id = await _task(db, patient["patient_id"], hours_old=5)
    gateway = FakeGateway()

    result = await _run(session_factory, pager=gateway.pager())
    assert (result["escalated"], result["paged"], result["page_failures"]) == (1, 1, 0)
    [request] = gateway.requests
    body = request.content
    stamp, digest = (part.split("=", 1)[1] for part in request.headers[SIGNATURE_HEADER].split(","))
    assert digest == sign(SECRET, stamp, body)
    assert request.headers["Idempotency-Key"] == f"{DEFAULT_TENANT}:{task_id}:1"
    page = json.loads(body)
    assert page["task_id"] == task_id and page["priority"] == "high" and page["overdue_minutes"] >= 59
    for phi in ("Jane", "Doe", "pneumonia", "walker", patient["patient_id"]):
        assert phi not in body.decode()
    for note in await db.scalars(select(Notification)):
        assert "Jane" not in note.message and "walker" not in note.message
    [escalation] = await _escalations(db, task_id)
    assert (escalation.page_status, escalation.page_attempts) == ("sent", 1)
    await _run(session_factory, pager=gateway.pager())
    assert len(gateway.requests) == 1  # a sent page is not sent again


async def test_failed_pages_are_retried_a_bounded_number_of_times(db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    task_id = await _task(db, (await create_patient(admin))["patient_id"], hours_old=5)
    gateway = FakeGateway(status=503)
    for _ in range(MAX_PAGE_ATTEMPTS + 2):
        await _run(session_factory, pager=gateway.pager())
    assert len(gateway.requests) == MAX_PAGE_ATTEMPTS
    [escalation] = await _escalations(db, task_id)
    assert (escalation.page_status, escalation.page_attempts) == ("failed", MAX_PAGE_ATTEMPTS)
    audits = [d for (d,) in await db.execute(
        select(AuditLog.details).where(AuditLog.action == "PAGE_ESCALATION", AuditLog.resource_id == task_id))]
    assert len(audits) == MAX_PAGE_ATTEMPTS and all("failed" in d for d in audits)


async def test_no_pager_configured_marks_pages_disabled(db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    task_id = await _task(db, (await create_patient(admin))["patient_id"], hours_old=5)
    await _run(session_factory)  # default pager: none
    [escalation] = await _escalations(db, task_id)
    assert escalation.page_status == "disabled"


async def test_read_only_tenant_is_skipped(db, session_factory, make_user, create_patient):
    user, admin = await make_user("admin")
    await _task(db, (await create_patient(admin))["patient_id"], hours_old=5)
    db.add(DowntimePeriod(kind="read_only", reason="Maintenance", started_by=user["user_id"]))
    await db.commit()
    result = await _run(session_factory)
    assert (result["skipped"], result["escalated"]) == ("read_only", 0)


@pytest.fixture
def cron(monkeypatch, session_factory):
    monkeypatch.setattr(escalations_router, "settings",
                        dataclasses.replace(escalations_router.settings, cron_secret=CRON))
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    yield {"Authorization": f"Bearer {CRON}"}
    app.dependency_overrides.pop(get_session_factory, None)


async def test_run_endpoint_authentication(client, db, make_user, create_patient, cron, make_tenant):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    await make_tenant("other")
    await _task(db, (await create_patient(admin))["patient_id"], hours_old=5)

    assert (await client.get("/api/escalations/run")).status_code == 401
    assert (await client.get("/api/escalations/run", headers=nurse)).status_code == 401
    assert (await client.get("/api/escalations/run", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.post("/api/escalations/run", headers=nurse)).status_code == 403
    assert (await client.post("/api/escalations/run")).status_code == 401

    resp = await client.get("/api/escalations/run", headers=cron)
    assert resp.status_code == 200, resp.text
    by_tenant = {r["tenant_id"]: r for r in resp.json()["tenants"]}
    assert by_tenant[DEFAULT_TENANT]["escalated"] == 1 and by_tenant["other"]["escalated"] == 0
    assert (await client.post("/api/escalations/run", headers=cron)).json()["tenants"][0]["escalated"] == 0

    resp = await client.post("/api/escalations/run", headers=admin)
    assert resp.status_code == 200 and [r["tenant_id"] for r in resp.json()["tenants"]] == [DEFAULT_TENANT]
    key = (await client.post("/api/integrations/clients", json={"name": "Scheduler"}, headers=admin)).json()["api_key"]
    resp = await client.post("/api/escalations/run", headers={"X-API-Key": key})
    assert resp.status_code == 200, resp.text
    bad = {"X-API-Key": key[:-2] + "xx"}
    assert (await client.post("/api/escalations/run", headers=bad)).status_code == 401


async def test_cron_is_off_without_a_secret(client, make_user):
    _, admin = await make_user("admin")
    assert escalations_router.settings.cron_secret is None
    assert (await client.get("/api/escalations/run", headers={"Authorization": "Bearer "})).status_code == 401
    assert (await client.get("/api/escalations/run", headers=admin)).status_code == 401


async def test_admin_view_of_active_escalations(client, db, session_factory, make_user, create_patient):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    unit = (await client.post("/api/units", json={"code": "4W", "name": "Four West"}, headers=admin)).json()
    pid = (await create_patient(admin, unit_id=unit["unit_id"]))["patient_id"]
    task_id = await _task(db, pid, hours_old=5)
    await _run(session_factory)

    assert (await client.get("/api/escalations", headers=nurse)).status_code == 403
    resp = await client.get("/api/escalations", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [row] = body["escalations"]
    assert (row["task_id"], row["unit_code"], row["priority_name"], row["level"]) == (task_id, "4W", "high", 1)
    assert "title" not in row and body["policy"]["high"]["sla_hours"] == 4 and body["pager"] == "none"

    task = await db.get(Task, task_id)
    task.status = "completed"
    await db.commit()
    assert (await client.get("/api/escalations", headers=admin)).json()["escalations"] == []


async def test_escalation_run_is_a_write_refused_in_read_only_mode(client, make_user):
    user, admin = await make_user("admin")
    assert (await client.post("/api/downtime/start", json={"reason": "Maintenance"}, headers=admin)).status_code == 200
    resp = await client.post("/api/escalations/run", headers=admin)
    assert resp.status_code == 503 and resp.headers["X-Read-Only"] == "true"


@requires_postgres
async def test_escalations_are_tenant_isolated_and_runs_do_not_overlap(
        db, session_factory, make_user, create_patient, make_tenant):
    _, admin = await make_user("admin")
    await make_tenant("other")
    _, other_admin = await make_user("admin", tenant_id="other")
    await _task(db, (await create_patient(admin))["patient_id"], hours_old=5)
    token = current_tenant_id.set("other")
    try:
        async with session_factory() as other_db:
            await _task(other_db, (await create_patient(other_admin))["patient_id"], hours_old=5)
    finally:
        current_tenant_id.reset(token)

    result = await run_all_tenants(session_factory)
    assert {r["tenant_id"]: r["escalated"] for r in result["tenants"]} == {DEFAULT_TENANT: 1, "other": 1}

    async with session_factory() as session:
        await use_tenant(session, "other")
        assert await session.scalar(text("SELECT current_user")) == "dischargeflow_app"
        tenants = set(await session.scalars(select(Escalation.tenant_id)))
        assert tenants == {"other"}

    # A run that finds another run holding the tenant's lock skips instead of racing it.
    async with session_factory() as holder:
        await use_tenant(holder, DEFAULT_TENANT)
        assert await holder.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                   {"k": f"dischargeflow:escalate:{DEFAULT_TENANT}"})
        assert (await _run(session_factory))["skipped"] == "already_running"
        await holder.rollback()
    assert (await _run(session_factory))["skipped"] is None
