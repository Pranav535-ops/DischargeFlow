"""ADR-002 Phase 7, item 6: read-only (downtime) mode, the discharge census, and backfilling paper records."""
import csv
import dataclasses
import io
import re
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select, update

from app.audit.models import AuditLog
from app.core.db import utcnow
from app.discharge.models import Task, Workflow
from app.downtime import service as downtime_service
from app.main import app
from app.notifications.models import Notification
from app.patients.models import Patient

pytestmark = pytest.mark.anyio

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT = re.compile(r"^/api/(auth/|downtime/end$)")


async def audit_rows(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


async def start_downtime(client, admin, reason="Database maintenance until 02:00"):
    resp = await client.post("/api/downtime/start", json={"reason": reason}, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def record_outage(client, admin, start_hours_ago=4, end_hours_ago=1):
    now = utcnow()
    resp = await client.post("/api/downtime/periods", json={
        "started_at": (now - timedelta(hours=start_hours_ago)).isoformat(),
        "ended_at": (now - timedelta(hours=end_hours_ago)).isoformat(),
        "reason": "Network outage on the 4th floor",
    }, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def write_routes():
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api"):
            for method in sorted(route.methods & WRITE_METHODS):
                yield method, re.sub(r"\{[^}]+\}", "x", route.path)


# ---------- read-only mode ----------

async def test_read_only_mode_refuses_every_write_route(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    assert (await client.get("/api/downtime/status", headers=nurse)).json() == {
        "read_only": False, "forced": False, "period": None}
    assert (await client.post("/api/downtime/start", json={"reason": "maintenance"}, headers=nurse)).status_code == 403

    period = await start_downtime(client, admin)
    assert period["kind"] == "read_only" and period["ended_at"] is None
    status = (await client.get("/api/downtime/status", headers=nurse)).json()
    assert status["read_only"] is True and status["period"]["reason"] == "Database maintenance until 02:00"
    assert (await client.post("/api/downtime/start", json={"reason": "again"}, headers=admin)).status_code == 503

    # Every write route, whatever its own auth or body: refused before it runs. An integration key is refused too.
    checked = 0
    for method, path in write_routes():
        if EXEMPT.match(path):
            continue  # signing out and ending the downtime are tested below
        resp = await client.request(method, path, json={}, headers={**admin, "X-API-Key": "default.cli_x.secret"})
        assert resp.status_code == 503, f"{method} {path}: {resp.status_code}"
        assert resp.headers["X-Read-Only"] == "true" and "read-only" in resp.json()["detail"]
        checked += 1
    assert checked > 25
    hl7 = await client.post("/api/hl7/adt", content="MSH|", headers={"X-API-Key": "default.cli_x.secret"})
    assert hl7.status_code == 503

    # Reads keep working, and are still audited.
    reads_before = len(await audit_rows(db, "PHI_READ"))
    assert (await client.get(f"/api/patients/{patient['patient_id']}", headers=nurse)).status_code == 200
    assert len(await audit_rows(db, "PHI_READ")) == reads_before + 1
    # Signing out still works.
    assert (await client.post("/api/auth/logout", headers=nurse)).status_code != 503

    ended = await client.post("/api/downtime/end", headers=admin)
    assert ended.status_code == 200 and ended.json()["ended_at"]
    assert (await client.post("/api/downtime/end", headers=admin)).status_code == 409
    assert (await client.get("/api/downtime/status", headers=admin)).json()["read_only"] is False
    _, nurse2 = await make_user("nurse")
    assert (await client.put(f"/api/patients/{patient['patient_id']}", json={"room_number": "14B"},
                             headers=nurse2)).status_code == 200

    assert [e.resource_id for e in await audit_rows(db, "START_DOWNTIME")] == [period["downtime_id"]]
    assert [e.resource_id for e in await audit_rows(db, "END_DOWNTIME")] == [period["downtime_id"]]
    titles = set(await db.scalars(select(Notification.title)))
    assert {"Downtime: DischargeFlow is read-only", "Downtime over: DischargeFlow is writable again"} <= titles


async def test_read_only_mode_is_per_tenant(client, make_user, make_tenant, create_patient):
    await make_tenant("hospital_b")
    _, admin_a = await make_user("admin")
    _, nurse_b = await make_user("nurse", tenant_id="hospital_b")
    await start_downtime(client, admin_a)
    assert (await client.get("/api/downtime/status", headers=nurse_b)).json()["read_only"] is False
    await create_patient(nurse_b)


async def test_config_forces_read_only(client, make_user, monkeypatch):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    forced = dataclasses.replace(downtime_service.settings, read_only_mode=True)
    monkeypatch.setattr(downtime_service, "settings", forced)

    status = (await client.get("/api/downtime/status", headers=nurse)).json()
    assert status == {"read_only": True, "forced": True, "period": None}
    assert (await client.post("/api/patients", json={}, headers=nurse)).status_code == 503
    # Refused before authentication, so it holds even for callers without credentials.
    assert (await client.post("/api/patients", json={})).status_code == 503
    # The admin switch can't lift it: ending the (absent) database period changes nothing.
    assert (await client.post("/api/downtime/end", headers=admin)).status_code == 409
    assert (await client.get("/api/health")).status_code == 200


# ---------- census ----------

async def test_census_by_unit_json_and_csv(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    units = {}
    for code in ("4W", "ICU"):
        resp = await client.post("/api/units", json={"code": code, "name": f"Unit {code}"}, headers=admin)
        units[code] = resp.json()["unit_id"]
    p1 = await create_patient(nurse, name="=HYPERLINK(\"http://x\")", room_number="2", unit_id=units["4W"])
    p2 = await create_patient(nurse, name="Ann Lee", room_number="1", unit_id=units["4W"])
    await create_patient(nurse, name="Ivy Chen", unit_id=units["ICU"])
    await create_patient(nurse, name="No Unit")
    gone = await create_patient(nurse, name="Gone Home", unit_id=units["4W"])
    wf = (await client.post(f"/api/workflows/initiate/{p1['patient_id']}", headers=physician)).json()
    await db.execute(update(Patient).where(Patient.patient_id == gone["patient_id"]).values(status="discharged"))
    await db.commit()

    resp = await client.get("/api/census", params={"unit_id": units["4W"]}, headers=nurse)
    assert resp.status_code == 200, resp.text
    census = resp.json()
    assert census["unit"]["code"] == "4W"
    assert [r["name"] for r in census["patients"]] == ["Ann Lee", p1["name"]]  # by room; discharged left out
    row = census["patients"][1]
    assert row["discharge_status"] == "in_progress" and row["workflow_id"] == wf["workflow"]["workflow_id"]
    assert len(row["open_tasks"]) == 8 and row["open_tasks"][0]["priority"] == 1
    assert census["patients"][0]["discharge_status"] == "not_started" and census["patients"][0]["open_tasks"] == []
    assert census["totals"] == {"patients": 2, "in_discharge": 1, "pending_approval": 0, "open_tasks": 8}
    assert p2["patient_id"] in {r["patient_id"] for r in census["patients"]}

    everyone = (await client.get("/api/census", headers=nurse)).json()
    assert [r["unit_code"] for r in everyone["patients"]] == ["4W", "4W", "ICU", None]
    unassigned = (await client.get("/api/census", params={"unit_id": "unassigned"}, headers=nurse)).json()
    assert [r["name"] for r in unassigned["patients"]] == ["No Unit"]
    assert (await client.get("/api/census", params={"unit_id": "unit_nope"}, headers=nurse)).status_code == 404

    resp = await client.get("/api/census", params={"unit_id": units["4W"], "format": "csv"}, headers=nurse)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert re.search(r'filename="discharge-census-4W-\d{8}-\d{4}\.csv"', resp.headers["content-disposition"])
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][:3] == ["unit", "room", "patient"] and len(rows) == 3
    assert rows[2][2].startswith("'=HYPERLINK")  # spreadsheet formulas are neutralised
    assert rows[2][12] == "8" and "Final Medication Review (pharmacist, high)" in rows[2][13]

    # One PHI_READ entry per request, naming the scope and count, never a patient.
    reads = [e for e in await audit_rows(db, "PHI_READ") if "census" in (e.details or "")]
    assert [e.details for e in reads] == [
        "discharge census, 4W (2 records, json)",
        "discharge census, all (4 records, json)",
        "discharge census, unassigned (1 records, json)",
        "discharge census, 4W (2 records, csv)",
    ]
    assert all(e.resource_id is None for e in reads)


async def test_census_works_in_read_only_mode(client, make_user, create_patient):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    await create_patient(nurse)
    await start_downtime(client, admin)
    resp = await client.get("/api/census", params={"format": "csv"}, headers=nurse)
    assert resp.status_code == 200 and len(resp.text.splitlines()) == 2


# ---------- backfill ----------

async def _workflow_tasks(client, make_user, create_patient, db, hours_ago=5):
    """A patient in discharge whose workflow and tasks were created `hours_ago` (so a past downtime can cover them)."""
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    wf_id = (await client.post(f"/api/workflows/initiate/{patient['patient_id']}",
                               headers=physician)).json()["workflow"]["workflow_id"]
    earlier = utcnow() - timedelta(hours=hours_ago)
    await db.execute(update(Task).where(Task.workflow_id == wf_id).values(created_at=earlier))
    await db.execute(update(Workflow).where(Workflow.workflow_id == wf_id).values(started_at=earlier))
    await db.commit()
    tasks = list(await db.scalars(select(Task).where(Task.workflow_id == wf_id)))
    return physician, nurse, patient, wf_id, tasks


async def test_backfill_task_inside_a_recorded_outage(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    physician, nurse, _, _, tasks = await _workflow_tasks(client, make_user, create_patient, db)
    _, pharmacist = await make_user("pharmacist")
    nurse_task = next(t for t in tasks if t.assigned_role == "nurse")
    done_at = utcnow() - timedelta(hours=2)
    path = f"/api/tasks/{nurse_task.task_id}/backfill"

    # No downtime recorded yet: nothing to back-enter into.
    resp = await client.put(path, json={"completed_at": done_at.isoformat()}, headers=nurse)
    assert resp.status_code == 400 and "downtime period" in resp.json()["detail"]

    assert (await client.post("/api/downtime/periods", json={
        "started_at": utcnow().isoformat(), "ended_at": (utcnow() - timedelta(hours=1)).isoformat(),
        "reason": "backwards"}, headers=admin)).status_code == 400
    assert (await client.post("/api/downtime/periods", json={
        "started_at": utcnow().isoformat(), "ended_at": (utcnow() + timedelta(hours=1)).isoformat(),
        "reason": "future"}, headers=admin)).status_code == 400
    outage = await record_outage(client, admin)
    assert outage["kind"] == "outage"
    assert (await client.post("/api/downtime/periods", json={
        "started_at": (utcnow() - timedelta(hours=3)).isoformat(),
        "ended_at": (utcnow() - timedelta(minutes=30)).isoformat(), "reason": "overlap"},
        headers=admin)).status_code == 409
    assert (await client.post("/api/downtime/periods", json={
        "started_at": (utcnow() - timedelta(hours=3)).isoformat(),
        "ended_at": (utcnow() - timedelta(minutes=30)).isoformat(), "reason": "nurse"},
        headers=nurse)).status_code == 403
    listed = (await client.get("/api/downtime/periods", headers=nurse)).json()
    assert [p["downtime_id"] for p in listed] == [outage["downtime_id"]]

    bad_times = {
        "naive": (done_at.replace(tzinfo=None).isoformat(), 422),
        "future": ((utcnow() + timedelta(minutes=5)).isoformat(), 400),
        "outside": ((utcnow() - timedelta(minutes=10)).isoformat(), 400),
        "before task": ((utcnow() - timedelta(hours=6)).isoformat(), 400),
    }
    for label, (value, code) in bad_times.items():
        assert (await client.put(path, json={"completed_at": value}, headers=nurse)).status_code == code, label
    # Same ownership rules as a live update: another role needs task:override and a reason.
    assert (await client.put(path, json={"completed_at": done_at.isoformat()}, headers=pharmacist)).status_code == 403

    resp = await client.put(path, json={"completed_at": done_at.isoformat()}, headers=nurse)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed" and body["backfilled_at"]
    assert body["completed_at"].startswith(done_at.isoformat()[:19])
    assert (await client.put(path, json={"completed_at": done_at.isoformat()}, headers=nurse)).status_code == 400

    other = next(t for t in tasks if t.assigned_role == "pharmacist")
    opath = f"/api/tasks/{other.task_id}/backfill"
    assert (await client.put(opath, json={"completed_at": done_at.isoformat()}, headers=physician)).status_code == 400
    resp = await client.put(opath, json={"completed_at": done_at.isoformat(), "reason": "Paper MAR signed by RPh"},
                            headers=physician)
    assert resp.status_code == 200 and resp.json()["override_reason"] == "Paper MAR signed by RPh"

    entries = await audit_rows(db, "BACKFILL_TASK_STATUS")
    assert [e.resource_id for e in entries] == [nurse_task.task_id, other.task_id]
    assert f"(downtime {outage['downtime_id']})" in entries[0].details
    assert "override of assigned role pharmacist" in entries[1].details and "MAR" not in entries[1].details
    assert [e.action for e in await audit_rows(db, "RECORD_OUTAGE")] == ["RECORD_OUTAGE"]

    # A live status change clears the backfill marker.
    resp = await client.put(f"/api/tasks/{nurse_task.task_id}", json={"status": "pending"}, headers=nurse)
    assert resp.status_code == 200, resp.text
    assert resp.json()["backfilled_at"] is None and resp.json()["completed_at"] is None


async def test_backfill_into_a_read_only_period_and_discharge(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    physician, nurse, patient, wf_id, tasks = await _workflow_tasks(client, make_user, create_patient, db)
    _, pharmacist = await make_user("pharmacist")
    by_role = {"physician": physician, "nurse": nurse, "pharmacist": pharmacist}

    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": patient["patient_id"],
                                                                   "workflow_id": wf_id}, headers=physician)).json()
    assert (await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)).status_code == 200

    period = await start_downtime(client, admin)
    # Read-only periods start now; move this one back so the paper work below falls inside it, then end it.
    await db.execute(update(downtime_service.DowntimePeriod)
                     .where(downtime_service.DowntimePeriod.downtime_id == period["downtime_id"])
                     .values(started_at=utcnow() - timedelta(hours=3)))
    await db.commit()
    done_at = utcnow() - timedelta(hours=2)
    open_task = next(t for t in tasks if t.status != "completed" and t.assigned_role == "nurse")
    resp = await client.put(f"/api/tasks/{open_task.task_id}/backfill",
                            json={"completed_at": done_at.isoformat()}, headers=nurse)
    assert resp.status_code == 503  # nothing is entered until the downtime is over
    assert (await client.post("/api/downtime/end", headers=admin)).status_code == 200

    for task in tasks:
        current = await client.get(f"/api/workflows/{wf_id}", headers=nurse)
        state = next(t for t in current.json()["tasks"] if t["task_id"] == task.task_id)
        if state["status"] != "completed":
            resp = await client.put(f"/api/tasks/{task.task_id}/backfill", json={"completed_at": done_at.isoformat()},
                                    headers=by_role[task.assigned_role])
            assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/workflows/{wf_id}", headers=nurse)).json()["status"] == "pending_approval"

    discharged_at = utcnow() - timedelta(hours=1, minutes=30)
    resp = await client.put(f"/api/workflows/{wf_id}/complete",
                            json={"completed_at": (utcnow() + timedelta(minutes=5)).isoformat()}, headers=physician)
    assert resp.status_code == 400
    resp = await client.put(f"/api/workflows/{wf_id}/complete", json={"completed_at": discharged_at.isoformat()},
                            headers=physician)
    assert resp.status_code == 200, resp.text
    workflow = await db.get(Workflow, wf_id)
    await db.refresh(workflow)
    assert workflow.status == "completed" and workflow.backfilled_at is not None
    assert abs((workflow.completed_at - discharged_at).total_seconds()) < 1
    entry = (await audit_rows(db, "COMPLETE_DISCHARGE"))[-1]
    assert f"backfilled: discharged at {discharged_at.isoformat()} (downtime {period['downtime_id']})" in entry.details
