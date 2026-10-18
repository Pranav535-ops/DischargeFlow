"""ADR-002 Phase 7, item 1: approval-gated sign-up, task ownership, claim permissions, write and PHI-read audit coverage."""
import dataclasses

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.chatbot.tools import TOOLS
from app.identity import router as identity_router
from app.identity.models import User
from app.notifications.models import Notification

pytestmark = pytest.mark.anyio


def sign_in_as(monkeypatch, email: str, name: str = "Google User"):
    async def fake_profile(session_id: str) -> dict:
        return {"email": email, "name": name, "picture": None}

    monkeypatch.setattr(identity_router, "fetch_oauth_profile", fake_profile)


def configure(monkeypatch, **changes):
    monkeypatch.setattr(identity_router, "settings", dataclasses.replace(identity_router.settings, **changes))


async def audit_rows(db, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.seq)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(await db.scalars(stmt))


# ---------- 1. sign-up needs approval ----------

async def test_new_sign_in_is_pending_and_sees_no_data(client, monkeypatch, make_user, create_patient, db):
    _, admin = await make_user("admin")
    await create_patient(admin)
    sign_in_as(monkeypatch, "stranger@gmail.example")
    resp = await client.post("/api/auth/session", json={"session_id": "s1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["role"] == "pending"
    bearer = {"Authorization": f"Bearer {body['token']}"}

    assert (await client.get("/api/auth/me", headers=bearer)).json()["role"] == "pending"
    for path in ("/api/patients", "/api/workflows", "/api/tasks", "/api/tasks/my", "/api/insurance/claims",
                 "/api/notifications", "/api/analytics/dashboard", "/api/fhir/R4/Patient"):
        resp = await client.get(path, headers=bearer)
        assert resp.status_code == 403, path
        assert resp.json()["detail"] == "Account pending approval"

    # Admins are told, and the request is audited.
    admin_notes = (await client.get("/api/notifications", headers=admin)).json()
    assert any(n["title"] == "Access Request" and n["link"] == "/admin" for n in admin_notes)
    [entry] = await audit_rows(db, "REQUEST_ACCESS")
    assert entry.resource_id == body["user"]["user_id"]

    # Approval takes effect immediately (identity cache invalidated).
    resp = await client.put(f"/api/users/{body['user']['user_id']}/role", json={"role": "nurse"}, headers=admin)
    assert resp.status_code == 200
    assert (await client.get("/api/patients", headers=bearer)).status_code == 200

    # ...and `pending` can be set again to suspend someone.
    assert (await client.put(f"/api/users/{body['user']['user_id']}/role", json={"role": "pending"},
                             headers=admin)).status_code == 200
    assert (await client.get("/api/patients", headers=bearer)).status_code == 403


async def test_pending_user_seeded_directly_is_refused(client, make_user):
    _, pending = await make_user("pending")
    assert (await client.get("/api/patients", headers=pending)).status_code == 403
    assert (await client.get("/api/auth/me", headers=pending)).status_code == 200


async def test_domain_allow_list(client, monkeypatch, db):
    configure(monkeypatch, allowed_email_domains=("hospital.example",))
    sign_in_as(monkeypatch, "someone@gmail.example")
    resp = await client.post("/api/auth/session", json={"session_id": "s"})
    assert resp.status_code == 403
    assert "domain" in resp.json()["detail"]
    assert await db.scalar(select(User).where(User.email == "someone@gmail.example")) is None

    sign_in_as(monkeypatch, "Dr.Who@Hospital.Example")
    resp = await client.post("/api/auth/session", json={"session_id": "s"})
    assert resp.status_code == 200 and resp.json()["user"]["role"] == "pending"


async def test_existing_users_keep_their_role_whatever_the_settings(client, monkeypatch, db):
    """The production admin (and every existing user) signs in as before, even outside the allow-list."""
    db.add(User(email="owner@gmail.example", name="Owner", role="admin"))
    await db.commit()
    configure(monkeypatch, allowed_email_domains=("hospital.example",), self_registration=False)
    sign_in_as(monkeypatch, "Owner@gmail.example", "Owner")
    resp = await client.post("/api/auth/session", json={"session_id": "s"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"] == "admin"
    bearer = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert (await client.get("/api/users", headers=bearer)).status_code == 200


async def test_invite_only(client, monkeypatch, make_user, db):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    configure(monkeypatch, self_registration=False)

    sign_in_as(monkeypatch, "uninvited@hospital.example")
    resp = await client.post("/api/auth/session", json={"session_id": "s"})
    assert resp.status_code == 403 and "invited" in resp.json()["detail"]

    invite = {"email": "New.Pharmacist@hospital.example", "role": "pharmacist"}
    assert (await client.post("/api/users", json=invite, headers=nurse)).status_code == 403
    assert (await client.post("/api/users", json={**invite, "role": "ceo"}, headers=admin)).status_code == 400
    resp = await client.post("/api/users", json=invite, headers=admin)
    assert resp.status_code == 200, resp.text
    invited_id = resp.json()["user_id"]
    assert resp.json()["name"] == "New.Pharmacist"
    dup = await client.post("/api/users", json={**invite, "email": "new.pharmacist@HOSPITAL.example"}, headers=admin)
    assert dup.status_code == 409
    [entry] = await audit_rows(db, "INVITE_USER")
    assert entry.resource_id == invited_id and entry.details == "role=pharmacist"

    # The invited person's first sign-in (any letter case) gets the invited role and their Google name.
    sign_in_as(monkeypatch, "new.pharmacist@hospital.example", "Pat Pharmacist")
    resp = await client.post("/api/auth/session", json={"session_id": "s"})
    assert resp.status_code == 200, resp.text
    user = resp.json()["user"]
    assert user["user_id"] == invited_id and user["role"] == "pharmacist" and user["name"] == "Pat Pharmacist"
    bearer = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert (await client.get("/api/patients", headers=bearer)).status_code == 200


# ---------- 2. task ownership ----------

async def _workflow_tasks(client, physician, create_patient, nurse):
    pid = (await create_patient(nurse))["patient_id"]
    wf = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    tasks = (await client.get(f"/api/workflows/{wf}", headers=nurse)).json()["tasks"]
    return {t["title"]: t for t in tasks}


async def test_only_the_assignee_changes_task_status(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    _, pharmacist = await make_user("pharmacist")
    tasks = await _workflow_tasks(client, physician, create_patient, nurse)
    med = tasks["Final Medication Review"]["task_id"]

    resp = await client.put(f"/api/tasks/{med}", json={"status": "completed"}, headers=nurse)
    assert resp.status_code == 403
    resp = await client.put(f"/api/tasks/{med}", json={"status": "completed", "reason": "covering"}, headers=nurse)
    assert resp.status_code == 403  # a reason does not stand in for task:override
    resp = await client.put(f"/api/tasks/{med}", json={"status": "in_progress"}, headers=pharmacist)
    assert resp.status_code == 200 and resp.json()["override_reason"] is None

    [entry] = await audit_rows(db, "UPDATE_TASK_STATUS")
    assert entry.resource_id == med and entry.details == "pending → in_progress"


async def test_physician_override_needs_a_reason_which_is_stored(client, make_user, create_patient, db):
    physician_doc, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    tasks = await _workflow_tasks(client, physician, create_patient, nurse)
    vitals = tasks["Final Vitals Check"]["task_id"]

    resp = await client.put(f"/api/tasks/{vitals}", json={"status": "completed"}, headers=physician)
    assert resp.status_code == 400 and "reason" in resp.json()["detail"]
    resp = await client.put(f"/api/tasks/{vitals}", json={"status": "completed", "reason": "  "}, headers=physician)
    assert resp.status_code == 400

    reason = "Vitals taken by me during the ward round"
    resp = await client.put(f"/api/tasks/{vitals}", json={"status": "completed", "reason": reason}, headers=physician)
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["override_reason"] == reason and task["override_by"] == physician_doc["user_id"]
    assert task["completed_by"] == physician_doc["user_id"]

    [entry] = await audit_rows(db, "OVERRIDE_TASK_STATUS")
    assert entry.resource_id == vitals and "pending → completed" in entry.details
    assert reason not in entry.details  # free text stays on the task row, not in immutable audit rows

    # Setting the same status again is a no-op, not a second override.
    resp = await client.put(f"/api/tasks/{vitals}", json={"status": "completed"}, headers=physician)
    assert resp.status_code == 200
    assert len(await audit_rows(db, "OVERRIDE_TASK_STATUS")) == 1


async def test_reassignment_needs_task_assign(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    nurse_doc, nurse = await make_user("nurse")
    other_nurse_doc, other_nurse = await make_user("nurse")
    pharmacist_doc, pharmacist = await make_user("pharmacist")
    pending_doc, _ = await make_user("pending")
    tasks = await _workflow_tasks(client, physician, create_patient, nurse)
    education = tasks["Patient Education"]["task_id"]
    med = tasks["Final Medication Review"]["task_id"]

    # A nurse may claim a nurse task, but not hand it (or another role's task) to someone else.
    resp = await client.put(f"/api/tasks/{education}", json={"assigned_to": nurse_doc["user_id"]}, headers=nurse)
    assert resp.status_code == 200 and resp.json()["assigned_to"] == nurse_doc["user_id"]
    resp = await client.put(f"/api/tasks/{education}", json={"assigned_to": other_nurse_doc["user_id"]}, headers=nurse)
    assert resp.status_code == 403
    assert (await client.put(f"/api/tasks/{med}", json={"assigned_to": nurse_doc["user_id"]},
                             headers=nurse)).status_code == 403

    # A physician (task:assign) may assign to any active user, but not to an unknown or pending account.
    for bad in ("user_nope", pending_doc["user_id"]):
        assert (await client.put(f"/api/tasks/{med}", json={"assigned_to": bad}, headers=physician)).status_code == 400
    resp = await client.put(f"/api/tasks/{med}", json={"assigned_to": pharmacist_doc["user_id"]}, headers=physician)
    assert resp.status_code == 200

    # The named assignee completes it; a status change that fails rolls back the reassignment in the same request.
    resp = await client.put(f"/api/tasks/{education}",
                            json={"assigned_to": pharmacist_doc["user_id"], "status": "completed"}, headers=nurse)
    assert resp.status_code == 403
    assert (await client.put(f"/api/tasks/{med}", json={"status": "completed"}, headers=pharmacist)).status_code == 200

    entries = await audit_rows(db, "REASSIGN_TASK")
    assert [e.resource_id for e in entries] == [education, med]
    assert entries[0].details == f"role nurse → {nurse_doc['user_id']}"


# ---------- 3. insurance claims ----------

async def test_claim_writes_need_claim_manage(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    _, physician = await make_user("physician")
    _, pharmacist = await make_user("pharmacist")
    _, admin = await make_user("admin")
    pid = (await create_patient(nurse))["patient_id"]
    claim = {"patient_id": pid, "insurer_name": "Acme Health", "policy_number": "P-1", "billed_amount": 1200.0}

    for headers in (physician, pharmacist):
        assert (await client.post("/api/insurance/claims", json=claim, headers=headers)).status_code == 403
    resp = await client.post("/api/insurance/claims", json=claim, headers=nurse)
    assert resp.status_code == 200, resp.text
    claim_id = resp.json()["claim_id"]

    for headers in (physician, pharmacist):
        assert (await client.put(f"/api/insurance/claims/{claim_id}/status", json={"status": "submitted"},
                                 headers=headers)).status_code == 403
    for headers in (nurse, admin):
        assert (await client.put(f"/api/insurance/claims/{claim_id}/status", json={"status": "submitted"},
                                 headers=headers)).status_code == 200

    # Reads stay open to every signed-in role.
    assert len((await client.get("/api/insurance/claims", headers=pharmacist)).json()) == 1
    assert (await client.get("/api/insurance/reports", headers=physician)).status_code == 200
    assert TOOLS["submit_claim"].permission == "claim:manage"


# ---------- 4. audit coverage ----------

async def test_patient_and_workflow_writes_are_audited_without_phi(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    pid = patient["patient_id"]
    assert (await client.put(f"/api/patients/{pid}", json={"room_number": "7B", "diagnosis": "Sepsis"},
                             headers=nurse)).status_code == 200

    wf = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf},
                                 headers=physician)).json()
    assert (await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)).status_code == 200
    for task in (await client.get(f"/api/workflows/{wf}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed", "reason": "test"},
                             headers=physician)
    assert (await client.put(f"/api/workflows/{wf}/complete", headers=physician)).status_code == 200

    rows = await audit_rows(db)
    by_action = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)
    assert by_action["CREATE_PATIENT"][0].resource_id == pid
    assert by_action["UPDATE_PATIENT"][0].details == "fields: diagnosis, room_number"
    [initiated] = by_action["INITIATE_DISCHARGE"]
    assert initiated.resource_id == pid and wf in initiated.details
    [completed] = by_action["COMPLETE_DISCHARGE"]
    assert completed.resource_id == pid and completed.details == f"workflow={wf}"
    assert len(by_action["OVERRIDE_TASK_STATUS"]) == 7
    # The patient's audit trail now shows the whole discharge.
    trail = {e["action"] for e in (await client.get(f"/api/audit/patients/{pid}", headers=physician)).json()}
    assert {"CREATE_PATIENT", "UPDATE_PATIENT", "INITIATE_DISCHARGE", "COMPLETE_DISCHARGE", "PHI_READ"} <= trail

    for row in rows:
        for phi in (patient["name"], patient["diagnosis"], "Sepsis", patient["date_of_birth"]):
            assert phi not in (row.details or ""), (row.action, row.details)


async def test_phi_reads_are_audited_once_per_request(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    pids = [(await create_patient(nurse, name=f"Patient {i}"))["patient_id"] for i in range(3)]
    pid = pids[0]

    assert len((await client.get("/api/patients", headers=nurse)).json()) == 3
    [list_read] = await audit_rows(db, "PHI_READ")
    assert list_read.resource_id is None and list_read.details == "patient list (3 records)"
    assert list_read.resource_type == "patient" and list_read.ip_address

    await client.get(f"/api/patients/{pid}", headers=nurse)
    wf = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
    await client.get(f"/api/workflows/{wf}", headers=nurse)
    await client.get(f"/api/workflows/patient/{pid}", headers=nurse)
    assert (await client.get(f"/api/ai/summary/{wf}", headers=nurse)).json() is None  # nothing read, nothing logged
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf},
                                 headers=physician)).json()
    await client.get(f"/api/ai/summary/{wf}", headers=nurse)
    # Failed reads are not logged (the entry shares the request's transaction).
    assert (await client.get("/api/patients/pat_missing", headers=nurse)).status_code == 404
    assert (await client.get("/api/workflows/wf_missing", headers=nurse)).status_code == 404

    reads = await audit_rows(db, "PHI_READ")
    assert [(r.resource_id, r.details) for r in reads[1:]] == [
        (pid, "patient record"),
        (pid, f"workflow {wf} (with tasks and summary)"),
        (pid, f"workflow {wf} (active, with tasks and summary)"),
        (pid, f"discharge summary {summary['summary_id']}"),
    ]
    assert all(r.user_name == "Test nurse" for r in reads)
    for r in reads:
        assert "Patient " not in r.details


async def test_admin_notification_rows_are_created_for_access_requests(client, monkeypatch, make_user, db):
    admin_doc, _ = await make_user("admin")
    sign_in_as(monkeypatch, "applicant@hospital.example")
    assert (await client.post("/api/auth/session", json={"session_id": "s"})).status_code == 200
    notes = list(await db.scalars(select(Notification).where(Notification.user_id == admin_doc["user_id"])))
    assert [n.title for n in notes] == ["Access Request"]
