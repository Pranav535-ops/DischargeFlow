"""Right to erasure: an erased patient's record takes no new PHI or clinical work, and consent needs patient:write."""
import pytest
from sqlalchemy import select

from app.ai.llm import MockLLMClient
from app.chatbot import router as chatbot_router
from app.chatbot.models import AssistantAction
from app.core.db import utcnow
from app.discharge.models import Task, Workflow
from app.patients.models import REDACTED, Encounter, ErasedPatientError, Patient
from app.patients.service import ERASED_DETAIL
from app.risk import service as risk

pytestmark = pytest.mark.anyio


async def _erase(client, make_user, pid):
    _, admin = await make_user("admin")
    resp = await client.delete(f"/api/patients/{pid}/data", headers=admin)
    assert resp.status_code == 200, resp.text
    return admin


def _refused(resp):
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == ERASED_DETAIL


async def test_writes_to_an_erased_patient_are_refused(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    pid = (await create_patient(physician, name="Ada Lovelace"))["patient_id"]
    resp = await client.post(f"/api/workflows/initiate/{pid}", headers=physician)
    wf_id = resp.json()["workflow"]["workflow_id"]
    enc = (await db.get(Workflow, wf_id)).encounter_id
    tasks_before = len((await db.scalars(select(Task).where(Task.patient_id == pid))).all())
    admin = await _erase(client, make_user, pid)

    _refused(await client.put(f"/api/patients/{pid}", headers=nurse,
                              json={"name": "Ada Lovelace", "diagnosis": "Pneumonia", "room_number": "2B"}))
    _refused(await client.post("/api/ai/generate-summary", headers=physician,
                               json={"patient_id": pid, "workflow_id": wf_id, "clinical_notes": "Stable."}))
    _refused(await client.post("/api/tasks", headers=nurse, json={
        "patient_id": pid, "title": "Give meds", "description": "", "category": "medication",
        "assigned_role": "nurse"}))
    _refused(await client.post("/api/insurance/claims", headers=nurse, json={
        "patient_id": pid, "insurer_name": "X", "policy_number": "P-1", "billed_amount": 1}))
    _refused(await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                              json={"planned_disposition": "home"}))
    _refused(await client.post(f"/api/encounters/{enc}/plan/review", headers=nurse))
    _refused(await client.put(f"/api/encounters/{enc}/risk-inputs", headers=nurse,
                              json={"comorbidities": ["congestive_heart_failure"]}))
    _refused(await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse,
                               json={"keys": [risk.SUGGESTED_KEYS[0]]}))

    db.expire_all()
    patient = await db.get(Patient, pid)
    assert patient.phi_dek is None and patient.name == REDACTED and patient.diagnosis == REDACTED
    assert patient.room_number != "2B"
    assert len((await db.scalars(select(Task).where(Task.patient_id == pid))).all()) == tasks_before
    shown = (await client.get(f"/api/patients/{pid}", headers=nurse)).json()
    assert shown["name"] == REDACTED and shown["gdpr_erased"] is True

    # Erasure stays idempotent.
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200


async def test_initiating_discharge_for_an_erased_patient_is_refused(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    await _erase(client, make_user, pid)
    _refused(await client.post(f"/api/workflows/initiate/{pid}", headers=physician))
    assert (await db.scalars(select(Workflow).where(Workflow.patient_id == pid))).all() == []


async def test_assistant_write_for_a_patient_erased_after_the_proposal_is_refused(client, make_user, create_patient,
                                                                                 db, monkeypatch):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse, name="Ada Lovelace"))["patient_id"]
    monkeypatch.setattr(chatbot_router, "assistant_client", lambda: MockLLMClient())
    resp = await client.post("/api/chatbot/message", headers=nurse,
                             json={"message": "create a task for Ada Lovelace to give meds"})
    [proposal] = resp.json()["pending_actions"]
    await _erase(client, make_user, pid)

    _refused(await client.post(f"/api/chatbot/actions/{proposal['action_id']}/confirm", headers=nurse))
    assert (await db.scalars(select(Task).where(Task.patient_id == pid))).all() == []
    action = await db.get(AssistantAction, proposal["action_id"])
    assert action.status == "pending"  # the refused confirm rolled back


def test_write_phi_refuses_an_erased_patient():
    patient = Patient(patient_id="pat_erasedtest", tenant_id="default", name="Ada Lovelace", date_of_birth="1990-01-01",
                      diagnosis="Pneumonia", admission_date="2026-01-01", room_number="1A", attending_physician="Dr. X")
    patient.shred()
    for field in ("name", "date_of_birth", "diagnosis", "mrn"):
        with pytest.raises(ErasedPatientError):
            setattr(patient, field, "new value")
    assert patient.phi_dek is None and patient.name == REDACTED

    encounter = Encounter(encounter_id="enc_erasedtest", tenant_id="default", patient=patient,
                          patient_id=patient.patient_id, admitted_at=utcnow())
    with pytest.raises(ErasedPatientError):
        encounter.visit_number = "V1"
    with pytest.raises(ErasedPatientError):
        encounter.comorbidities = ["congestive_heart_failure"]
    assert patient.phi_dek is None


async def test_consent_requires_patient_write(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    for role in ("pharmacist",):
        _, headers = await make_user(role)
        resp = await client.post(f"/api/patients/{pid}/consent", json={"gdpr_consent": False}, headers=headers)
        assert resp.status_code == 403, (role, resp.text)
    assert (await db.get(Patient, pid)).gdpr_consent is None
    resp = await client.post(f"/api/patients/{pid}/consent", json={"gdpr_consent": True}, headers=nurse)
    assert resp.status_code == 200
