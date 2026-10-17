"""The discharge state machine end to end: initiate -> tasks -> summary approval -> complete."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai import service as ai_service
from app.discharge.models import Workflow
from app.notifications.models import Notification

pytestmark = pytest.mark.anyio


@pytest.fixture
def stub_llm(monkeypatch):
    async def fake_generate(prompt: str, session_id: str) -> str:
        assert "Jane Doe" in prompt
        return "DISCHARGE SUMMARY\nHospital course: uneventful."

    monkeypatch.setattr(ai_service, "generate_summary_text", fake_generate)


async def test_full_discharge_happy_path(client, make_user, create_patient, stub_llm, db):
    _, physician = await make_user("physician")
    nurse_doc, nurse = await make_user("nurse")
    _, pharmacist = await make_user("pharmacist")
    patient = await create_patient(nurse)
    pid = patient["patient_id"]

    resp = await client.post(f"/api/workflows/initiate/{pid}", headers=physician)
    assert resp.status_code == 200, resp.text
    wf_id = resp.json()["workflow"]["workflow_id"]
    assert resp.json()["tasks_created"] == 8

    assert (await client.get(f"/api/patients/{pid}", headers=nurse)).json()["status"] == "pending_discharge"
    nurse_notes = (await client.get("/api/notifications", headers=nurse)).json()
    assert any(n["title"] == "New Discharge Initiated" for n in nurse_notes)

    # Second initiation for the same patient is rejected.
    assert (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).status_code == 400

    # Cannot complete while tasks are open.
    resp = await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    assert resp.status_code == 400 and "tasks still pending" in resp.json()["detail"]

    # Generate and approve the summary; approval completes the documentation task.
    resp = await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf_id}, headers=physician)
    assert resp.status_code == 200, resp.text
    summary_id = resp.json()["summary_id"]
    assert (await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf_id},
                              headers=physician)).status_code == 400
    assert (await client.put(f"/api/ai/approve-summary/{summary_id}", headers=physician)).status_code == 200

    detail = (await client.get(f"/api/workflows/{wf_id}", headers=nurse)).json()
    assert detail["discharge_summary"]["approved"] is True
    assert [t["priority"] for t in detail["tasks"]] == sorted(t["priority"] for t in detail["tasks"])
    doc_task = next(t for t in detail["tasks"] if t["title"] == "Complete Discharge Summary")
    assert doc_task["status"] == "completed"

    # Complete the remaining tasks; the workflow moves to pending_approval and physicians are told.
    for task in detail["tasks"]:
        if task["status"] != "completed":
            resp = await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"}, headers=pharmacist)
            assert resp.status_code == 200
            assert resp.json()["completed_by"]
    assert (await client.get(f"/api/workflows/{wf_id}", headers=nurse)).json()["status"] == "pending_approval"
    physician_notes = (await client.get("/api/notifications", headers=physician)).json()
    assert any(n["title"] == "Discharge Ready for Approval" for n in physician_notes)

    resp = await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/patients/{pid}", headers=nurse)).json()["status"] == "discharged"
    assert (await client.get(f"/api/workflows/patient/{pid}", headers=nurse)).json() is None

    # Completing twice is rejected.
    assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 400

    stats = (await client.get("/api/analytics/dashboard", headers=nurse)).json()
    assert stats["patients"]["discharged"] == 1
    assert stats["workflows"]["completed_today"] == 1
    assert stats["metrics"]["avg_discharge_time_hours"] is not None


async def test_complete_requires_approved_summary(client, make_user, create_patient):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    wf_id = (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)).json()["workflow"]["workflow_id"]
    for task in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]:
        await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"}, headers=physician)

    resp = await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Discharge summary not approved"


async def test_reopening_or_adding_task_returns_workflow_to_in_progress(client, make_user, create_patient):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    pid = patient["patient_id"]
    wf_id = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    tasks = (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]
    for task in tasks:
        await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"}, headers=physician)
    assert (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["status"] == "pending_approval"

    await client.put(f"/api/tasks/{tasks[0]['task_id']}", json={"status": "pending"}, headers=physician)
    assert (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["status"] == "in_progress"

    await client.put(f"/api/tasks/{tasks[0]['task_id']}", json={"status": "completed"}, headers=physician)
    resp = await client.post("/api/tasks", headers=physician, json={
        "patient_id": pid, "title": "Arrange transport", "description": "Wheelchair taxi",
        "category": "follow_up", "assigned_role": "nurse", "priority": 1,
    })
    assert resp.json()["workflow_id"] == wf_id
    assert (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["status"] == "in_progress"


async def test_manual_task_without_workflow_has_null_workflow(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    resp = await client.post("/api/tasks", headers=nurse, json={
        "patient_id": patient["patient_id"], "title": "Check vitals", "description": "",
        "category": "documentation", "assigned_role": "nurse",
    })
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] is None
    my = (await client.get("/api/tasks/my", headers=nurse)).json()
    assert [t["title"] for t in my] == ["Check vitals"]
    assert (await client.get("/api/analytics/tasks-by-role", headers=nurse)).json() == {"nurse": 1}


async def test_task_for_unknown_patient_is_404(client, make_user):
    _, nurse = await make_user("nurse")
    resp = await client.post("/api/tasks", headers=nurse, json={
        "patient_id": "pat_missing", "title": "x", "description": "", "category": "general", "assigned_role": "nurse",
    })
    assert resp.status_code == 404


async def test_invalid_task_status_rejected(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    task = (await client.post("/api/tasks", headers=nurse, json={
        "patient_id": patient["patient_id"], "title": "x", "description": "", "category": "general",
        "assigned_role": "nurse",
    })).json()
    resp = await client.put(f"/api/tasks/{task['task_id']}", json={"status": "done-ish"}, headers=nurse)
    assert resp.status_code == 422


async def test_database_enforces_one_active_workflow_per_patient(db, make_user, create_patient):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    db.add_all([
        Workflow(patient_id=patient["patient_id"], initiated_by="u1", status="in_progress"),
        Workflow(patient_id=patient["patient_id"], initiated_by="u2", status="pending_approval"),
    ])
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # Completed workflows don't count toward the limit.
    db.add_all([
        Workflow(patient_id=patient["patient_id"], initiated_by="u1", status="completed"),
        Workflow(patient_id=patient["patient_id"], initiated_by="u2", status="in_progress"),
    ])
    await db.commit()
    assert len((await db.scalars(select(Workflow))).all()) == 2


async def test_summary_returns_503_when_ai_not_configured(client, make_user, create_patient):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    wf_id = (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)).json()["workflow"]["workflow_id"]
    resp = await client.post("/api/ai/generate-summary",
                             json={"patient_id": patient["patient_id"], "workflow_id": wf_id}, headers=physician)
    assert resp.status_code == 503
    assert (await client.get(f"/api/ai/summary/{wf_id}", headers=physician)).json() is None


async def test_failed_request_rolls_back_all_writes(client, make_user, create_patient, db, monkeypatch):
    """An error mid-request must not leave half a workflow behind (one transaction per request)."""
    from app.discharge import service

    _, physician = await make_user("physician")
    patient = await create_patient(physician)

    async def boom(*args, **kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(service, "notify_role", boom)
    with pytest.raises(RuntimeError):
        await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)

    assert (await db.scalars(select(Workflow))).all() == []
    assert (await db.scalars(select(Notification))).all() == []
    assert (await client.get(f"/api/patients/{patient['patient_id']}", headers=physician)).json()["status"] == "admitted"
