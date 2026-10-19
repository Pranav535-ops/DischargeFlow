"""ADR-002 Phase 8, items 8-10: discharge plan reviewed daily, workflow templates per disposition with prerequisites
and due times, and delay reason codes."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.audit.models import AuditLog
from app.core.db import utcnow
from app.core.tenancy import current_tenant_id, use_access_scope, use_tenant
from app.discharge import templates
from app.discharge.models import DelayReason, Task, WorkflowTemplate
from app.patients.models import REDACTED, Encounter
from tests.conftest import DEFAULT_TENANT, requires_postgres

pytestmark = pytest.mark.anyio

TODAY = date.today()


@pytest.fixture
async def default_templates(session_factory):
    token = current_tenant_id.set(DEFAULT_TENANT)
    try:
        async with session_factory() as session:
            assert await templates.ensure_default_templates(session) == len(templates.DEFAULT_TEMPLATES)
            await session.commit()
    finally:
        current_tenant_id.reset(token)


async def _encounter_id(client, headers, patient_id) -> str:
    [encounter] = (await client.get(f"/api/patients/{patient_id}/encounters", headers=headers)).json()
    return encounter["encounter_id"]


async def _audit(db, action) -> list[AuditLog]:
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


# ---------- item 8: the plan and its daily review ----------

async def test_plan_is_set_reviewed_and_audited_without_values(client, make_user, create_patient, db):
    nurse_doc, nurse = await make_user("nurse", name="Nurse Plan")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)

    resp = await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={
        "expected_discharge_date": (TODAY + timedelta(days=2)).isoformat(), "planned_disposition": "snf"})
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["planned_disposition"] == "snf" and plan["reviewed_today"] is True
    assert plan["plan_reviewed_by"] == nurse_doc["user_id"] and plan["plan_reviewed_by_name"] == "Nurse Plan"

    [entry] = await _audit(db, "UPDATE_DISCHARGE_PLAN")
    assert entry.resource_type == "patient" and entry.resource_id == pid
    assert entry.details == f"encounter={enc} fields: expected_discharge_date, planned_disposition"
    assert "snf" not in entry.details and str(TODAY.year) not in entry.details

    # Unknown disposition, and roles without plan:manage.
    assert (await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                             json={"planned_disposition": "moon"})).status_code == 422
    _, pharmacist = await make_user("pharmacist")
    assert (await client.post(f"/api/encounters/{enc}/plan/review", headers=pharmacist)).status_code == 403
    assert (await client.get(f"/api/encounters/{enc}/plan", headers=pharmacist)).status_code == 200


async def test_review_list_groups_by_unit_and_shows_who_reviewed_today(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    unit = (await client.post("/api/units", json={"code": "4W", "name": "4 West"}, headers=admin)).json()["unit_id"]
    physician_doc, physician = await make_user("physician", name="Dr Rounds")
    west = (await create_patient(physician, name="West Patient", unit_id=unit))["patient_id"]
    loose = (await create_patient(physician, name="Loose Patient"))["patient_id"]
    await client.post(f"/api/encounters/{await _encounter_id(client, physician, west)}/plan/review", headers=physician)

    body = (await client.get("/api/discharge-plans/review", headers=physician)).json()
    assert body["today"]
    by_unit = {g["unit_code"]: g for g in body["units"]}
    assert by_unit["4W"]["unit_name"] == "4 West" and by_unit["4W"]["not_reviewed_today"] == 0
    [reviewed] = by_unit["4W"]["plans"]
    assert reviewed["patient_name"] == "West Patient" and reviewed["reviewed_today"] is True
    assert reviewed["plan_reviewed_by_name"] == "Dr Rounds"
    [unassigned] = by_unit[None]["plans"]
    assert unassigned["patient_id"] == loose and unassigned["reviewed_today"] is False

    pending = (await client.get("/api/discharge-plans/review?pending_only=true", headers=physician)).json()
    assert [p["patient_id"] for g in pending["units"] for p in g["plans"]] == [loose]
    only_unit = (await client.get(f"/api/discharge-plans/review?unit_id={unit}", headers=physician)).json()
    assert [g["unit_code"] for g in only_unit["units"]] == ["4W"]
    reads = await _audit(db, "PHI_READ")
    assert any(r.details == "discharge plan review list (2 records)" and r.resource_id is None for r in reads)

    # Yesterday's review is not today's.
    await db.execute(update(Encounter).where(Encounter.patient_id == west)
                     .values(plan_reviewed_at=utcnow() - timedelta(days=1)))
    await db.commit()
    body = (await client.get("/api/discharge-plans/review?pending_only=true", headers=physician)).json()
    assert {p["patient_id"] for g in body["units"] for p in g["plans"]} == {west, loose}


async def _first_encounter(db, patient_id) -> Encounter:
    return await db.scalar(select(Encounter).where(Encounter.patient_id == patient_id))


async def test_a_discharged_stay_has_no_plan_to_change(client, make_user, create_patient):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    enc = await _encounter_id(client, physician, pid)
    await client.put(f"/api/patients/{pid}", json={"status": "discharged"}, headers=physician)
    resp = await client.put(f"/api/encounters/{enc}/plan", json={"planned_disposition": "home"}, headers=physician)
    assert resp.status_code == 400


# ---------- item 10: delay reasons for the expected date ----------

async def test_moving_the_expected_date_later_needs_a_delay_reason(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)
    first = TODAY + timedelta(days=1)
    assert (await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                             json={"expected_discharge_date": first.isoformat()})).status_code == 200
    # Earlier is fine; later is a slip.
    assert (await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                             json={"expected_discharge_date": TODAY.isoformat()})).status_code == 200
    later = {"expected_discharge_date": (TODAY + timedelta(days=3)).isoformat()}
    resp = await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json=later)
    assert resp.status_code == 400 and "delay reason" in resp.json()["detail"]
    resp = await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={**later, "delay_code": "other"})
    assert resp.status_code == 400  # `other` needs a note
    assert (await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                             json={**later, "delay_code": "late"})).status_code == 422

    resp = await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                            json={**later, "delay_code": "placement", "delay_note": "Waiting on bed at Oak SNF"})
    assert resp.status_code == 200
    [delay] = resp.json()["delays"]
    assert delay["kind"] == "expected_date_slip" and delay["code"] == "placement" and delay["slip_days"] == 3
    entry = (await _audit(db, "UPDATE_DISCHARGE_PLAN"))[-1]
    assert f"delay={delay['delay_id']} code=placement" in entry.details and "Oak" not in entry.details


async def test_reviewing_a_passed_date_needs_a_reason_once_a_day(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                     json={"expected_discharge_date": (TODAY - timedelta(days=2)).isoformat()})
    # Setting the plan counted as today's review; a passed date needs a reason only on the first review of a day.
    plan = (await client.get(f"/api/encounters/{enc}/plan", headers=nurse)).json()
    assert plan["expected_date_passed"] is True and plan["reviewed_today"] is True
    assert (await client.post(f"/api/encounters/{enc}/plan/review", headers=nurse)).status_code == 200


async def test_first_review_of_a_passed_date_records_the_slip(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)
    encounter = await _first_encounter(db, pid)
    encounter.expected_discharge_date = TODAY - timedelta(days=2)
    await db.commit()

    assert (await client.post(f"/api/encounters/{enc}/plan/review", headers=nurse)).status_code == 400
    resp = await client.post(f"/api/encounters/{enc}/plan/review", headers=nurse, json={"delay_code": "social"})
    assert resp.status_code == 200
    [delay] = resp.json()["delays"]
    assert delay["code"] == "social" and delay["slip_days"] == 2
    assert (await client.post(f"/api/encounters/{enc}/plan/review", headers=nurse)).status_code == 200


# ---------- item 9: templates ----------

def test_default_templates_are_valid_and_match_the_migration():
    for template in templates.DEFAULT_TEMPLATES:
        spec = templates.TemplateIn(**template)
        assert templates.SUMMARY_TITLE in {t.title for t in spec.tasks}
    path = next((Path(__file__).parents[1] / "alembic" / "versions").glob("*_0014_*.py"))
    module_spec = importlib.util.spec_from_file_location("migration_0014", path)
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    frozen = [{"disposition": d, "name": n, "tasks": [
        {"key": k, "title": t, "description": desc, "category": c, "assigned_role": r, "priority": p,
         "due_offset_hours": o, "depends_on": list(deps)} for k, t, desc, c, r, p, o, deps in tasks]}
        for d, n, tasks in migration.TEMPLATES]
    assert frozen == templates.DEFAULT_TEMPLATES


async def test_initiate_uses_the_template_for_the_planned_disposition(client, make_user, create_patient, db,
                                                                      default_templates):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    pid = (await create_patient(physician))["patient_id"]
    enc = await _encounter_id(client, physician, pid)
    expected = TODAY + timedelta(days=3)
    await client.put(f"/api/encounters/{enc}/plan", headers=physician,
                     json={"expected_discharge_date": expected.isoformat(), "planned_disposition": "snf"})

    out = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()
    assert out["template"]["disposition"] == "snf" and out["template"]["version"] == 1
    wf = (await client.get(f"/api/workflows/{out['workflow']['workflow_id']}", headers=physician)).json()
    assert wf["template_id"] == out["template"]["template_id"] and wf["plan"]["planned_disposition"] == "snf"
    tasks = {t["title"]: t for t in wf["tasks"]}
    assert len(tasks) == out["tasks_created"] == 9
    placement, auth = tasks["Secure SNF Placement"], tasks["Insurance Prior Authorization"]
    assert auth["depends_on"] == [placement["task_id"]]
    assert auth["blocked_by"] == [{"task_id": placement["task_id"], "title": "Secure SNF Placement"}]
    # Due times are relative to the start of the expected date (UTC in tests): placement 48 h before.
    assert placement["due_at"].startswith((expected - timedelta(days=2)).isoformat())
    assert tasks["Complete Discharge Summary"]["due_at"].startswith(f"{expected.isoformat()}T10:00")
    [entry] = await _audit(db, "INITIATE_DISCHARGE")
    assert f"template={out['template']['template_id']} v1" in entry.details

    # A prerequisite blocks starting and completing, for overrides too; completing it unblocks.
    resp = await client.put(f"/api/tasks/{auth['task_id']}", json={"status": "in_progress"}, headers=nurse)
    assert resp.status_code == 409 and resp.json()["detail"] == "Waiting on: Secure SNF Placement"
    resp = await client.put(f"/api/tasks/{auth['task_id']}", headers=physician,
                            json={"status": "completed", "reason": "Physician override in test"})
    assert resp.status_code == 409
    assert (await client.put(f"/api/tasks/{placement['task_id']}", json={"status": "completed"},
                             headers=nurse)).status_code == 200
    assert (await client.put(f"/api/tasks/{auth['task_id']}", json={"status": "in_progress"},
                             headers=nurse)).status_code == 200

    # Moving the date moves the open template tasks' due times (an earlier date is not a slip).
    sooner = expected - timedelta(days=1)
    await client.put(f"/api/encounters/{enc}/plan", headers=physician,
                     json={"expected_discharge_date": sooner.isoformat()})
    summary_task = await db.get(Task, tasks["Complete Discharge Summary"]["task_id"])
    await db.refresh(summary_task)
    assert summary_task.due_at.date() == sooner


async def test_without_a_plan_or_template_the_standard_checklist_is_used(client, make_user, create_patient, db,
                                                                         default_templates):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    out = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()
    assert out["template"] is None and out["tasks_created"] == 8

    other = (await create_patient(physician))["patient_id"]
    await client.put(f"/api/encounters/{await _encounter_id(client, physician, other)}/plan", headers=physician,
                     json={"planned_disposition": "transfer"})  # no default template for transfers
    assert (await client.post(f"/api/workflows/initiate/{other}", headers=physician)).json()["template"] is None


async def test_template_admin_api(client, make_user, db, default_templates):
    _, admin = await make_user("admin")
    _, physician = await make_user("physician")
    listed = (await client.get("/api/workflow-templates", headers=physician)).json()
    assert {t["disposition"] for t in listed} == {"home", "home_services", "snf", "rehab", "hospice", "ama"}

    body = {"disposition": "transfer", "name": "Transfer to another hospital", "tasks": [
        {"key": "accept", "title": "Accepting physician confirmed", "assigned_role": "physician", "priority": 1,
         "due_offset_hours": -4},
        {"key": "packet", "title": "Send records", "assigned_role": "nurse", "depends_on": ["accept"]},
    ]}
    assert (await client.post("/api/workflow-templates", json=body, headers=physician)).status_code == 403
    resp = await client.post("/api/workflow-templates", json=body, headers=admin)
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["version"] == 1 and created["tasks"][1]["depends_on"] == ["accept"]
    assert (await client.post("/api/workflow-templates", json=body, headers=admin)).status_code == 409

    for bad in (
        {**body, "tasks": [{**body["tasks"][0], "depends_on": ["packet"]}, body["tasks"][1]]},  # cycle
        {**body, "tasks": [body["tasks"][1]]},  # unknown prerequisite
        {**body, "tasks": [{**body["tasks"][0], "assigned_role": "admin_assistant"}]},
        {**body, "tasks": [body["tasks"][0], body["tasks"][0]]},  # duplicate keys
        {**body, "disposition": "space"},
    ):
        assert (await client.put(f"/api/workflow-templates/{created['template_id']}", json=bad,
                                 headers=admin)).status_code == 422

    resp = await client.put(f"/api/workflow-templates/{created['template_id']}", headers=admin,
                            json={**body, "name": "Transfer", "active": False})
    assert resp.json()["version"] == 2 and resp.json()["active"] is False
    resp = await client.put(f"/api/workflow-templates/{created['template_id']}", headers=admin,
                            json={**body, "disposition": "home"})
    assert resp.status_code == 400
    actions = [e.action for e in await db.scalars(select(AuditLog).where(
        AuditLog.resource_type == "workflow_template"))]
    assert actions == ["CREATE_WORKFLOW_TEMPLATE", "UPDATE_WORKFLOW_TEMPLATE"]

    # An inactive template is skipped: the standard checklist.
    assert await templates.template_for(db, "transfer") is None
    codes = (await client.get("/api/discharge-plans/codes", headers=physician)).json()
    assert "placement" in codes["delay_codes"] and "snf" in codes["dispositions"]


def test_due_time_of_a_task_created_after_it_gets_the_sla_target():
    now = utcnow()
    assert templates.due_at_for(None, 10, 1) is None and templates.due_at_for(TODAY, None, 1) is None
    late = templates.due_at_for(TODAY - timedelta(days=5), 10, 1, now)
    assert late == now + timedelta(hours=4)


# ---------- item 10: tasks past their due time ----------

async def _overdue_task(client, physician, create_patient, db) -> dict:
    pid = (await create_patient(physician))["patient_id"]
    wf_id = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    task = next(t for t in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]
                if t["assigned_role"] == "nurse")
    await db.execute(update(Task).where(Task.task_id == task["task_id"]).values(
        created_at=utcnow() - timedelta(days=2)))
    await db.commit()
    return task


async def test_completing_a_late_task_needs_a_delay_reason(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    task = await _overdue_task(client, physician, create_patient, db)
    wf = (await client.get(f"/api/workflows/{task['workflow_id']}", headers=nurse)).json()
    assert next(t for t in wf["tasks"] if t["task_id"] == task["task_id"])["overdue"] is True

    resp = await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"}, headers=nurse)
    assert resp.status_code == 400 and "delay reason" in resp.json()["detail"]
    resp = await client.put(f"/api/tasks/{task['task_id']}", headers=nurse,
                            json={"status": "completed", "delay_code": "pharmacy"})
    assert resp.status_code == 200
    [delay] = await db.scalars(select(DelayReason).where(DelayReason.task_id == task["task_id"]))
    assert delay.kind == "task_sla_breach" and delay.code == "pharmacy" and delay.encounter_id
    [entry] = await _audit(db, "RECORD_DELAY_REASON")
    assert entry.resource_id == task["task_id"] and entry.details.endswith("code=pharmacy")

    # Reopened and completed again: the reason is already on record.
    await client.put(f"/api/tasks/{task['task_id']}", json={"status": "pending"}, headers=nurse)
    assert (await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"},
                             headers=nurse)).status_code == 200


async def test_a_delay_reason_can_be_given_while_the_task_is_open(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    _, pharmacist = await make_user("pharmacist")
    task = await _overdue_task(client, physician, create_patient, db)
    url = f"/api/tasks/{task['task_id']}/delay-reason"
    assert (await client.post(url, json={"code": "transport"}, headers=pharmacist)).status_code == 403
    assert (await client.post(url, json={"code": "transport"}, headers=nurse)).status_code == 200
    assert (await client.post(url, json={"code": "transport"}, headers=nurse)).status_code == 409
    wf = (await client.get(f"/api/workflows/{task['workflow_id']}", headers=nurse)).json()
    assert next(t for t in wf["tasks"] if t["task_id"] == task["task_id"])["delay_recorded"] is True
    assert (await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"},
                             headers=nurse)).status_code == 200

    pid = (await create_patient(physician))["patient_id"]
    wf_id = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    fresh = (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"][0]
    resp = await client.post(f"/api/tasks/{fresh['task_id']}/delay-reason", json={"code": "medical"},
                             headers=physician)
    assert resp.status_code == 400  # not late yet


async def test_delay_metrics_count_codes_and_hide_small_group_averages(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    for days, code in ((2, "placement"), (4, "placement")):
        pid = (await create_patient(nurse))["patient_id"]
        enc = await _encounter_id(client, nurse, pid)
        await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={"expected_discharge_date": TODAY.isoformat()})
        await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={
            "expected_discharge_date": (TODAY + timedelta(days=days)).isoformat(), "delay_code": code,
            "delay_note": "Patient Jane Doe waiting"})
    delays = (await client.get("/api/analytics/operations", headers=admin)).json()["delays"]
    assert delays["recorded"] == 2
    assert delays["expected_date_slip"] == {"n": 2, "by_code": {"placement": 2}, "slip_days": {"n": 2}}
    assert delays["task_sla_breach"] == {"n": 0, "by_code": {}}
    assert "Jane" not in str(delays)

    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={"expected_discharge_date": TODAY.isoformat()})
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={
        "expected_discharge_date": (TODAY + timedelta(days=6)).isoformat(), "delay_code": "insurance"})
    delays = (await client.get("/api/analytics/operations", headers=admin)).json()["delays"]
    assert delays["expected_date_slip"]["slip_days"] == {"n": 3, "avg_days": 4.0}


async def test_erasure_redacts_delay_notes(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter_id(client, nurse, pid)
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={"expected_discharge_date": TODAY.isoformat()})
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse, json={
        "expected_discharge_date": (TODAY + timedelta(days=1)).isoformat(), "delay_code": "other",
        "delay_note": "Daughter Jane is travelling"})
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200
    [delay] = await db.scalars(select(DelayReason).where(DelayReason.patient_id == pid))
    await db.refresh(delay)
    assert delay.note == REDACTED and delay.code == "other"


def test_hospital_timezone_must_be_known(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.setenv("HOSPITAL_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(RuntimeError, match="HOSPITAL_TIMEZONE"):
        load_settings()
    monkeypatch.setenv("HOSPITAL_TIMEZONE", "Europe/London")
    assert load_settings().hospital_timezone == "Europe/London"


# ---------- row-level security (Postgres) ----------

@requires_postgres
async def test_templates_and_delays_are_tenant_isolated(client, make_user, make_tenant, create_patient, db,
                                                        default_templates):
    await make_tenant("hospital_b")
    _, nurse_a = await make_user("nurse")
    _, nurse_b = await make_user("nurse", tenant_id="hospital_b")
    _, admin_b = await make_user("admin", tenant_id="hospital_b")
    pid = (await create_patient(nurse_a))["patient_id"]
    enc = await _encounter_id(client, nurse_a, pid)
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse_a, json={"expected_discharge_date": TODAY.isoformat()})
    await client.put(f"/api/encounters/{enc}/plan", headers=nurse_a, json={
        "expected_discharge_date": (TODAY + timedelta(days=1)).isoformat(), "delay_code": "medical"})

    assert (await client.get("/api/workflow-templates", headers=nurse_b)).json() == []
    assert (await client.get(f"/api/encounters/{enc}/plan", headers=nurse_b)).status_code == 404
    assert (await client.get("/api/analytics/operations", headers=admin_b)).json()["delays"]["recorded"] == 0
    assert (await client.get("/api/discharge-plans/review", headers=nurse_b)).json()["units"] == []
    # Hospital B can have its own home template.
    body = {"disposition": "home", "name": "Home", "tasks": [{"key": "a", "title": "A", "assigned_role": "nurse"}]}
    assert (await client.post("/api/workflow-templates", json=body, headers=admin_b)).status_code == 200
    assert len(list(await db.scalars(select(WorkflowTemplate).where(WorkflowTemplate.disposition == "home")))) == 2


@requires_postgres
async def test_delays_and_review_list_follow_the_patients_unit_scope(client, make_user, create_patient,
                                                                    session_factory, monkeypatch):
    import dataclasses

    from app.core import security

    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, unit_scoped_access=True))
    _, admin = await make_user("admin")
    units = {}
    for code in ("4W", "ICU"):
        units[code] = (await client.post("/api/units", json={"code": code, "name": code}, headers=admin)).json()["unit_id"]
    nurse_doc, nurse = await make_user("nurse")
    await client.put(f"/api/users/{nurse_doc['user_id']}/units", json={"unit_ids": [units["4W"]]}, headers=admin)
    west = (await create_patient(admin, unit_id=units["4W"]))["patient_id"]
    icu = (await create_patient(admin, unit_id=units["ICU"]))["patient_id"]
    icu_enc = await _encounter_id(client, admin, icu)
    await client.put(f"/api/encounters/{icu_enc}/plan", headers=admin, json={"expected_discharge_date": TODAY.isoformat()})
    await client.put(f"/api/encounters/{icu_enc}/plan", headers=admin, json={
        "expected_discharge_date": (TODAY + timedelta(days=1)).isoformat(), "delay_code": "medical"})

    body = (await client.get("/api/discharge-plans/review", headers=nurse)).json()
    assert [p["patient_id"] for g in body["units"] for p in g["plans"]] == [west]
    assert (await client.get(f"/api/encounters/{icu_enc}/plan", headers=nurse)).status_code == 404
    assert (await client.put(f"/api/encounters/{icu_enc}/plan", headers=nurse,
                             json={"planned_disposition": "home"})).status_code == 404

    # The delay row itself is hidden under the nurse's scope (RESTRICTIVE unit_scope policy on delay_reasons).
    async with session_factory() as scoped:
        await use_tenant(scoped, DEFAULT_TENANT)
        await use_access_scope(scoped, nurse_doc["user_id"], [units["4W"]])
        assert await scoped.scalar(select(func.count()).select_from(DelayReason)) == 0
        await use_access_scope(scoped, nurse_doc["user_id"], [units["4W"], units["ICU"]])
        assert await scoped.scalar(select(func.count()).select_from(DelayReason)) == 1
