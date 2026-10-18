"""Operational metrics: throughput, time waiting for approval, task SLA breaches (aggregate only, admin)."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import utcnow
from app.discharge.models import Task, Workflow
from app.discharge.service import set_workflow_status

pytestmark = pytest.mark.anyio


async def _discharge(client, physician, patient_id) -> str:
    """Initiate, approve the summary, complete every task; leaves the workflow in pending_approval."""
    wf_id = (await client.post(f"/api/workflows/initiate/{patient_id}", headers=physician)).json()["workflow"]["workflow_id"]
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": patient_id, "workflow_id": wf_id},
                                 headers=physician)).json()
    await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)
    for task in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed", "reason": "Physician override in test"}, headers=physician)
    return wf_id


async def test_pending_approval_time_is_accumulated_across_stays():
    wf = Workflow(patient_id="pat_x", initiated_by="u", status="in_progress", pending_approval_seconds=0)
    set_workflow_status(wf, "pending_approval")
    wf.pending_approval_since -= timedelta(hours=2)
    set_workflow_status(wf, "in_progress")  # a task was reopened
    assert wf.pending_approval_since is None and wf.pending_approval_seconds == pytest.approx(7200, abs=5)
    set_workflow_status(wf, "pending_approval")
    wf.pending_approval_since -= timedelta(hours=1)
    set_workflow_status(wf, "completed")
    assert wf.pending_approval_seconds == pytest.approx(3 * 3600, abs=10)


async def test_untracked_workflows_stay_unknown_not_zero():
    wf = Workflow(patient_id="pat_x", initiated_by="u", status="pending_approval", pending_approval_seconds=None)
    set_workflow_status(wf, "completed")
    assert wf.pending_approval_seconds is None


async def test_operational_metrics(client, db, make_user, create_patient):
    _, admin = await make_user("admin")
    _, physician = await make_user("physician")
    names = []
    for i in range(3):
        p = await create_patient(physician, name=f"Ops Patient{i}")
        names.append(p["name"])
        wf_id = await _discharge(client, physician, p["patient_id"])
        wf = await db.get(Workflow, wf_id)
        assert wf.status == "pending_approval" and wf.pending_approval_since is not None
        wf.pending_approval_since = utcnow() - timedelta(hours=4)
        await db.commit()
        assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 200

    # One untracked legacy discharge and one open high-priority task well past its 4 h target.
    legacy = await create_patient(physician, name="Legacy Patient")
    legacy_wf = Workflow(patient_id=legacy["patient_id"], initiated_by="u", status="completed",
                         started_at=utcnow() - timedelta(hours=30), completed_at=utcnow())
    db.add(legacy_wf)
    await db.flush()
    legacy_wf.pending_approval_seconds = None  # as migration 0006 leaves rows from before tracking
    db.add(Task(patient_id=legacy["patient_id"], title="Overdue", assigned_role="pharmacist", priority=1,
                created_at=utcnow() - timedelta(hours=10)))
    # A completed task that took longer than its medium-priority (12 h) target.
    slow = (await db.scalars(select(Task).where(Task.assigned_role == "nurse", Task.status == "completed"))).first()
    slow.priority, slow.created_at = 2, slow.completed_at - timedelta(hours=13)
    await db.commit()

    assert (await client.get("/api/analytics/operations", headers=physician)).status_code == 403
    assert (await client.get("/api/analytics/operations?days=0", headers=admin)).status_code == 422
    resp = await client.get("/api/analytics/operations", headers=admin)
    assert resp.status_code == 200, resp.text
    ops = resp.json()
    raw = json.dumps(ops)
    assert not any(n in raw for n in names + ["Legacy Patient"]) and "pat_" not in raw and "wf_" not in raw

    assert ops["throughput"]["discharges_completed"] == 4
    assert sum(ops["throughput"]["completed_per_day"].values()) == 4
    wait = ops["pending_approval"]["wait"]
    assert wait["n"] == 3 and wait["avg_hours"] == pytest.approx(4, abs=0.1)
    assert ops["pending_approval"]["untracked_completed"] == 1
    assert ops["pending_approval"]["waiting_now"] == 0

    sla = ops["task_sla"]
    assert sla["targets_hours"] == {"high": 4, "medium": 12, "low": 24}
    assert sla["open_breaching_by_role"] == {"pharmacist": {"high": 1}}
    assert sla["completed_by_role"]["nurse"]["breached"] == 1
    assert sla["completed"]["breached"] == 1 and "breach_rate" in sla["completed"]
