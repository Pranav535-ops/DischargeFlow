"""ADR-002 Phase 8, item 7: patients -> encounters -> workflows (one stay per encounter, readmission = new stay)."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.audit.models import AuditLog
from app.core import crypto
from app.core.db import utcnow
from app.discharge.models import Workflow
from app.patients.models import REDACTED, Encounter, Patient, admitted_at_from
from tests.conftest import requires_postgres

pytestmark = pytest.mark.anyio


async def _ready_for_discharge(client, physician, patient_id) -> str:
    """Initiate, approve the summary and complete every task; leaves the workflow in pending_approval."""
    wf_id = (await client.post(f"/api/workflows/initiate/{patient_id}", headers=physician)).json()["workflow"]["workflow_id"]
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": patient_id, "workflow_id": wf_id},
                                 headers=physician)).json()
    await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)
    for task in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            await client.put(f"/api/tasks/{task['task_id']}",
                             json={"status": "completed", "reason": "Physician override in test"}, headers=physician)
    return wf_id


def test_admitted_at_from_admission_date_strings():
    assert admitted_at_from("2026-10-01").isoformat() == "2026-10-01T00:00:00+00:00"
    assert admitted_at_from("2026-10-01T08:30:00").isoformat() == "2026-10-01T08:30:00+00:00"
    assert admitted_at_from("Not provided by ADT") is None and admitted_at_from(None) is None


async def test_patient_starts_with_one_active_encounter(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    resp = await client.get(f"/api/patients/{patient['patient_id']}/encounters", headers=nurse)
    assert resp.status_code == 200, resp.text
    [enc] = resp.json()
    assert enc["status"] == "active" and enc["encounter_id"].startswith("enc_")
    assert enc["admitted_at"] == "2026-10-01T00:00:00+00:00" and enc["room_number"] == "12A"
    assert enc["discharged_at"] is None and enc["workflows"] == []
    read = await db.scalar(select(AuditLog).where(AuditLog.action == "PHI_READ",
                                                  AuditLog.details.like("encounter history%")))
    assert read.resource_id == patient["patient_id"] and read.details == "encounter history (1 records)"
    assert (await client.get("/api/patients/pat_missing/encounters", headers=nurse)).status_code == 404


async def test_discharge_closes_the_encounter_of_its_workflow(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    wf_id = await _ready_for_discharge(client, physician, pid)
    workflow = (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()
    [enc] = (await client.get(f"/api/patients/{pid}/encounters", headers=physician)).json()
    assert workflow["encounter_id"] == enc["encounter_id"]
    assert enc["workflows"][0]["workflow_id"] == wf_id

    assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 200
    [enc] = (await client.get(f"/api/patients/{pid}/encounters", headers=physician)).json()
    assert enc["status"] == "discharged" and enc["discharged_at"]
    assert enc["workflows"] == [{"workflow_id": wf_id, "status": "completed", "started_at": workflow["started_at"],
                                 "completed_at": enc["workflows"][0]["completed_at"]}]
    # A discharged patient can't start another discharge until they are admitted again.
    resp = await client.post(f"/api/workflows/initiate/{pid}", headers=physician)
    assert resp.status_code == 400 and resp.json()["detail"] == "Patient is already discharged"


async def test_rows_from_the_previous_release_are_linked(client, make_user, db):
    """During the rollout the previous release writes patients without encounters and workflows without one."""
    _, physician = await make_user("physician")
    patient = Patient(name="Old Release", date_of_birth="1960-01-01", admission_date="2026-10-02",
                      room_number="3", diagnosis="COPD", attending_physician="Dr P")
    db.add(patient)
    await db.commit()
    pid = patient.patient_id

    wf_id = await _ready_for_discharge(client, physician, pid)
    enc = await db.scalar(select(Encounter).where(Encounter.patient_id == pid))
    assert enc.status == "active" and enc.admitted_at.isoformat() == "2026-10-02T00:00:00+00:00"
    eid = enc.encounter_id
    await db.execute(update(Workflow).where(Workflow.workflow_id == wf_id).values(encounter_id=None))
    await db.commit()

    assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 200
    db.expire_all()
    assert (await db.get(Workflow, wf_id)).encounter_id == eid
    assert (await db.get(Encounter, eid)).status == "discharged"


async def test_manual_status_changes_keep_one_active_encounter(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    edit = await client.put(f"/api/patients/{pid}", json={"room_number": "7B"}, headers=physician)
    assert edit.status_code == 200
    [enc] = (await client.get(f"/api/patients/{pid}/encounters", headers=physician)).json()
    assert enc["room_number"] == "7B"

    await client.put(f"/api/patients/{pid}", json={"status": "discharged"}, headers=physician)
    await client.put(f"/api/patients/{pid}", json={"status": "admitted", "admission_date": "2026-10-09"},
                     headers=physician)
    second, first = (await client.get(f"/api/patients/{pid}/encounters", headers=physician)).json()
    assert first["status"] == "discharged" and first["room_number"] == "7B"
    assert second["status"] == "active" and second["admitted_at"].startswith("2026-10-09")


async def test_visit_number_is_encrypted_and_erased(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    pid = (await create_patient(admin))["patient_id"]
    enc = await db.scalar(select(Encounter).where(Encounter.patient_id == pid))
    enc.visit_number = "V100200"
    eid = enc.encounter_id
    await db.commit()
    raw = await db.scalar(text("SELECT visit_number FROM encounters WHERE encounter_id = :e"), {"e": eid})
    assert crypto.is_encrypted(raw) and "V100200" not in raw
    [listed] = (await client.get(f"/api/patients/{pid}/encounters", headers=admin)).json()
    assert listed["visit_number"] == "V100200"

    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200
    db.expire_all()
    assert (await db.get(Encounter, eid)).visit_number == REDACTED
    fhir = (await client.get(f"/api/fhir/R4/Encounter/{eid}", headers=admin)).json()
    assert [i["system"] for i in fhir["identifier"]] == ["urn:dischargeflow:id"] and "location" not in fhir


async def test_fhir_lists_each_stay_as_an_encounter(client, make_user, create_patient, db):
    _, physician = await make_user("physician")
    pid = (await create_patient(physician))["patient_id"]
    wf_id = await _ready_for_discharge(client, physician, pid)
    await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    await client.put(f"/api/patients/{pid}", json={"status": "admitted"}, headers=physician)

    bundle = (await client.get(f"/api/fhir/R4/Encounter?patient=Patient/{pid}", headers=physician)).json()
    current, past = [e["resource"] for e in bundle["entry"]]
    assert bundle["total"] == 2
    assert current["status"] == "in-progress" and current["reasonCode"] == [{"text": "Community-acquired pneumonia"}]
    assert past["status"] == "finished" and past["period"]["end"] and "reasonCode" not in past
    docs = (await client.get(f"/api/fhir/R4/DocumentReference?encounter=Encounter/{past['id']}",
                             headers=physician)).json()
    assert docs["total"] == 1
    empty = (await client.get(f"/api/fhir/R4/DocumentReference?encounter=Encounter/{current['id']}",
                              headers=physician)).json()
    assert empty["total"] == 0


async def test_stay_metrics_count_length_of_stay_and_readmissions(client, make_user, db):
    _, admin = await make_user("admin")
    now = utcnow()
    for i in range(3):
        patient = Patient(name=f"Stay Patient{i}", date_of_birth="1950-01-01", admission_date="2026-10-01",
                          room_number="1", diagnosis="x", attending_physician="Dr P", status="admitted")
        db.add(patient)
        await db.flush()
        db.add(Encounter(patient=patient, patient_id=patient.patient_id, status="discharged",
                         admitted_at=now - timedelta(days=6), discharged_at=now - timedelta(days=2)))
        if i == 0:  # back within 30 days
            db.add(Encounter(patient=patient, patient_id=patient.patient_id, admitted_at=now - timedelta(days=1)))
    await db.commit()

    ops = (await client.get("/api/analytics/operations", headers=admin)).json()
    assert ops["stays"] == {"length_of_stay_days": {"n": 3, "avg_days": 4.0, "median_days": 4.0},
                            "admissions": 4, "readmissions_within_30_days": 1}
    assert "Stay Patient" not in json.dumps(ops) and "pat_" not in json.dumps(ops)


async def test_one_active_encounter_per_patient_is_enforced(create_patient, make_user, db):
    from sqlalchemy.exc import IntegrityError

    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    patient = await db.get(Patient, pid)
    db.add(Encounter(patient=patient, patient_id=pid))
    with pytest.raises(IntegrityError):
        await db.commit()


# ---------- row-level security (Postgres) ----------

@requires_postgres
async def test_encounters_are_tenant_isolated(client, make_user, make_tenant, create_patient):
    await make_tenant("hospital_b")
    _, nurse_a = await make_user("nurse")
    _, nurse_b = await make_user("nurse", tenant_id="hospital_b")
    pid = (await create_patient(nurse_a))["patient_id"]
    [enc] = (await client.get(f"/api/patients/{pid}/encounters", headers=nurse_a)).json()

    assert (await client.get(f"/api/patients/{pid}/encounters", headers=nurse_b)).status_code == 404
    assert (await client.get(f"/api/fhir/R4/Encounter/{enc['encounter_id']}", headers=nurse_b)).status_code == 404
    assert (await client.get("/api/fhir/R4/Encounter", headers=nurse_b)).json()["total"] == 0


@requires_postgres
async def test_encounters_follow_the_patients_unit_scope(client, make_user, create_patient, monkeypatch):
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

    visible = (await client.get("/api/fhir/R4/Encounter", headers=nurse)).json()
    assert {e["resource"]["subject"]["reference"] for e in visible["entry"]} == {f"Patient/{west}"}
    resp = await client.get(f"/api/patients/{icu}/encounters", headers=nurse)
    assert resp.status_code == 403 and resp.json()["break_glass"] is True

    await client.post(f"/api/patients/{icu}/break-glass", json={"reason": "Covering ICU for a colleague"},
                      headers=nurse)
    assert len((await client.get(f"/api/patients/{icu}/encounters", headers=nurse)).json()) == 1
