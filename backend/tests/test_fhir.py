"""Read-only FHIR R4 API: resource shapes, search, drafts never exposed, erased patients redacted, reads audited."""
import base64

import pytest

pytestmark = pytest.mark.anyio

BASE = "/api/fhir/R4"


async def _discharge_with_summary(client, make_user, create_patient, approve=True):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    wf = (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)).json()["workflow"]
    summary = (await client.post("/api/ai/generate-summary", headers=physician, json={
        "patient_id": patient["patient_id"], "workflow_id": wf["workflow_id"],
        "clinical_notes": "Medications: amoxicillin 500 mg three times daily"})).json()
    if approve:
        await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)
    return physician, patient, wf, summary


async def test_capability_statement(client):
    resp = await client.get(f"{BASE}/metadata")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/fhir+json")
    body = resp.json()
    assert body["resourceType"] == "CapabilityStatement" and body["fhirVersion"] == "4.0.1"
    assert {r["type"] for r in body["rest"][0]["resource"]} == {"Patient", "Encounter", "Task", "DocumentReference"}


async def test_reads_require_authentication(client):
    assert (await client.get(f"{BASE}/Patient")).status_code == 401


async def test_patient_encounter_task_and_document(client, make_user, create_patient):
    headers, patient, wf, summary = await _discharge_with_summary(client, make_user, create_patient)
    pid, eid = patient["patient_id"], wf["encounter_id"]

    p = (await client.get(f"{BASE}/Patient/{pid}", headers=headers)).json()
    assert p["resourceType"] == "Patient" and p["id"] == pid and p["name"] == [{"text": "Jane Doe"}]
    assert p["birthDate"] == "1970-01-01" and p["active"] is True

    # The Encounter is the stay (ADR-002 item 7), not the discharge workflow.
    e = (await client.get(f"{BASE}/Encounter/{eid}", headers=headers)).json()
    assert e["id"] == eid and eid.startswith("enc_") and e["status"] == "in-progress" and e["class"]["code"] == "IMP"
    assert e["subject"] == {"reference": f"Patient/{pid}"} and e["period"]["start"] == "2026-10-01T00:00:00+00:00"
    assert e["location"] == [{"location": {"display": "12A"}}]
    assert e["reasonCode"] == [{"text": "Community-acquired pneumonia"}]

    tasks = (await client.get(f"{BASE}/Task?encounter=Encounter/{eid}", headers=headers)).json()
    assert tasks["resourceType"] == "Bundle" and tasks["type"] == "searchset" and tasks["total"] == 8
    first = tasks["entry"][0]["resource"]
    assert first["intent"] == "order" and first["encounter"] == {"reference": f"Encounter/{eid}"}
    statuses = {e["resource"]["description"]: e["resource"]["status"] for e in tasks["entry"]}
    assert statuses["Complete Discharge Summary"] == "completed" and statuses["Final Vitals Check"] == "requested"
    assert tasks["entry"][0]["fullUrl"].endswith(f"/api/fhir/R4/Task/{first['id']}")

    docs = (await client.get(f"{BASE}/DocumentReference?patient={pid}", headers=headers)).json()
    [doc] = [e["resource"] for e in docs["entry"]]
    assert doc["docStatus"] == "final" and doc["type"]["coding"][0]["code"] == "18842-5"
    assert doc["context"]["encounter"] == [{"reference": f"Encounter/{eid}"}]
    text = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode("utf-8")
    assert text.startswith("DISCHARGE SUMMARY") and "amoxicillin" in text


async def test_drafts_are_not_documents(client, make_user, create_patient):
    headers, patient, _, summary = await _discharge_with_summary(client, make_user, create_patient, approve=False)
    assert (await client.get(f"{BASE}/DocumentReference/{summary['summary_id']}", headers=headers)).status_code == 404
    bundle = (await client.get(f"{BASE}/DocumentReference?patient=Patient/{patient['patient_id']}", headers=headers)).json()
    assert bundle["total"] == 0


async def test_erased_patient_exposes_no_identifiers(client, make_user, create_patient):
    _, admin = await make_user("admin")
    patient = await create_patient(admin)
    await client.delete(f"/api/patients/{patient['patient_id']}/data", headers=admin)
    p = (await client.get(f"{BASE}/Patient/{patient['patient_id']}", headers=admin)).json()
    assert "name" not in p and "birthDate" not in p and p["active"] is False


async def test_not_found_is_an_operation_outcome(client, make_user):
    _, nurse = await make_user("nurse")
    resp = await client.get(f"{BASE}/Patient/pat_missing", headers=nurse)
    assert resp.status_code == 404 and resp.json()["resourceType"] == "OperationOutcome"


async def test_fhir_reads_are_audited(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    _, admin = await make_user("admin")
    patient = await create_patient(nurse)
    await client.get(f"{BASE}/Patient/{patient['patient_id']}", headers=nurse)
    await client.get(f"{BASE}/Patient?_count=10", headers=nurse)
    logs = (await client.get("/api/audit/logs?resource_type=fhir", headers=admin)).json()
    assert [entry["action"] for entry in logs] == ["FHIR_READ", "FHIR_READ"]
    assert any(f"Patient/{patient['patient_id']}" in entry["details"] for entry in logs)
