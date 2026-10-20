# ruff: noqa: F811  (the make_client and send fixtures are imported from test_hl7_adt)
"""ADR-002 item 11: A03 reconciliation, A02/A08/A11/A13/A40 and parking of out-of-order messages."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from app.ai.models import DischargeSummary, content_sha256
from app.audit.models import AuditLog
from app.core import crypto
from app.core.db import utcnow
from app.discharge.models import Task, Workflow
from app.interop import hl7
from app.interop.models import HL7Message, HL7ParkedMessage
from app.notifications.models import Notification
from app.patients.models import Encounter, Patient
from tests.conftest import requires_postgres
from tests.test_hl7_adt import MRN, adt, make_client, send, with_pv1  # noqa: F401

pytestmark = pytest.mark.anyio

OTHER_MRN = "MRN0099"


async def _patient(db, mrn: str = MRN) -> Patient:
    db.expire_all()
    return next(p for p in await db.scalars(select(Patient)) if p.mrn == mrn)


async def _start_discharge(client, make_user, patient_id: str) -> tuple[str, dict]:
    _, physician = await make_user("physician")
    resp = await client.post(f"/api/workflows/initiate/{patient_id}", headers=physician)
    assert resp.status_code == 200, resp.text
    return resp.json()["workflow"]["workflow_id"], physician


async def _finish_documentation(client, physician, patient_id: str, wf_id: str) -> None:
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": patient_id, "workflow_id": wf_id},
                                 headers=physician)).json()
    assert (await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)).status_code == 200
    for task in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            resp = await client.put(f"/api/tasks/{task['task_id']}",
                                    json={"status": "completed", "reason": "Done after the patient left"},
                                    headers=physician)
            assert resp.status_code == 200, resp.text


def _no_phi(entry: AuditLog) -> None:
    assert "Jane" not in entry.details and "Doe" not in entry.details and MRN not in entry.details


# ---------- parser ----------

@pytest.mark.parametrize("value,expected", [
    ("20261019143000", datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)),
    ("202610191430", datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)),
    ("20261019", datetime(2026, 10, 19, tzinfo=timezone.utc)),
    ("20261019143000.1234+0200", datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)),
    ("20261019143000-0500", datetime(2026, 10, 19, 19, 30, tzinfo=timezone.utc)),
    ("2026101914", datetime(2026, 10, 19, 14, tzinfo=timezone.utc)),
    ("20261319", None), ("2026101", None), ("202610191", None), ("20261019+02", None), ("abc", None),
    # Offsets timezone() can't hold, digits int() can't read, and offsets that leave the datetime range.
    ("20261019100000+9999", None), ("20261019100000+2400", None), ("20261019100000-2400", None),
    ("20261019100000+0060", None), ("20261019100000+2359", datetime(2026, 10, 18, 10, 1, tzinfo=timezone.utc)),
    ("20261019100000+²²00", None), ("2026101910²0", None),
    ("00010101000000+0100", None), ("99991231230000-0200", None),
])
def test_parse_datetime(value, expected):
    assert hl7.parse_datetime(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("19700101", "1970-01-01"), ("197001011230+0200", "1970-01-01"),
    ("19701301", None), ("1970010", None), ("1970²101", None), ("", None),
])
def test_parse_date(value, expected):
    assert hl7.parse_date(value) == expected


async def test_a03_with_an_impossible_offset_is_acknowledged(make_client, send, db):
    """A bad PV1-45 must not turn into a 500: the interface engine would resend it forever and stall the queue."""
    _, key, _ = await make_client()
    assert (await send(key, with_pv1(adt("A01", "TZ1"), f19="V1")))["code"] == "AA"
    ack = await send(key, with_pv1(adt("A03", "TZ2"), f19="V1", f45="20261019100000+9999"))
    assert ack["code"] == "AA"
    encounter = await db.scalar(select(Encounter))
    # PV1-45 is skipped; EVN-2 of the test message is the next valid time.
    assert encounter.discharged_at == datetime(2026, 10, 18, 11, 55, tzinfo=timezone.utc)


# ---------- A03 reconciliation ----------

async def test_a03_before_the_workflow_is_done_reconciles_then_completes(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    assert (await send(key, with_pv1(adt("A01", "R1"), f19="V1")))["code"] == "AA"
    patient = await _patient(db)
    wf_id, physician = await _start_discharge(client, make_user, patient.patient_id)
    _, nurse = await make_user("nurse")

    ack = await send(key, with_pv1(adt("A03", "R2"), f19="V1", f36="01", f45="20261019100000"))
    assert ack["code"] == "AA" and "workflow left open" in ack["text"]
    db.expire_all()
    workflow = await db.get(Workflow, wf_id)
    departed = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    assert workflow.status == "externally_discharged" and workflow.departed_at == departed
    assert workflow.documentation_due_at == departed + timedelta(hours=24)
    assert (await _patient(db)).status == "discharged"
    encounter = await db.scalar(select(Encounter))
    assert (encounter.status, encounter.discharged_at, encounter.disposition) == ("discharged", departed, "01")
    open_tasks = list(await db.scalars(select(Task).where(Task.workflow_id == wf_id, Task.status != "completed")))
    assert len(open_tasks) == 8  # nothing closed without being done

    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "EXTERNAL_DISCHARGE"))
    assert f"workflow={wf_id}" in entry.details and "open_tasks=8" in entry.details
    _no_phi(entry)
    for headers in (physician, nurse):
        titles = [n["title"] for n in (await client.get("/api/notifications", headers=headers)).json()]
        assert "Patient Left Before Discharge Was Complete" in titles
    detail = (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()
    assert detail["documentation_due_at"] == (departed + timedelta(hours=24)).isoformat()

    # A resend of the same message (lost ACK) replays the ACK and changes nothing.
    again = await send(key, with_pv1(adt("A03", "R2"), f19="V1", f36="01", f45="20261019100000"))
    assert again["text"] == ack["text"]
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_REPLAY"))
    # A new A03 for the same departure (new control id) finds nothing left to do.
    assert (await send(key, adt("A03", "R3")))["text"] == "Patient already discharged"

    # Can't complete while tasks are open; can once they are done, at the departure time.
    resp = await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    assert resp.status_code == 400 and "tasks still pending" in resp.json()["detail"]
    await _finish_documentation(client, physician, patient.patient_id, wf_id)
    db.expire_all()
    assert (await db.get(Workflow, wf_id)).status == "externally_discharged"  # tasks done: still waits for sign-off
    resp = await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)
    assert resp.status_code == 200, resp.text
    db.expire_all()
    workflow = await db.get(Workflow, wf_id)
    assert workflow.status == "completed" and workflow.completed_at == departed
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "COMPLETE_DISCHARGE"))
    assert "after external discharge" in entry.details


async def test_readmission_while_documentation_is_open_keeps_both(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    await send(key, with_pv1(adt("A01", "M1"), f19="V1"))
    patient = await _patient(db)
    wf_id, physician = await _start_discharge(client, make_user, patient.patient_id)
    await send(key, with_pv1(adt("A03", "M2"), f19="V1"))

    # Readmitted before the paperwork of the last stay is done: a new stay, the old workflow stays open.
    assert (await send(key, with_pv1(adt("A01", "M3"), f19="V2")))["text"] == "Patient readmitted"
    await _finish_documentation(client, physician, patient.patient_id, wf_id)
    assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 200
    patient = await _patient(db)
    assert patient.status == "admitted"  # completing the old workflow doesn't discharge the new stay
    statuses = sorted(e.status for e in await db.scalars(select(Encounter)))
    assert statuses == ["active", "discharged"]


async def test_backfill_time_is_refused_after_an_external_discharge(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "B1"))
    patient = await _patient(db)
    wf_id, physician = await _start_discharge(client, make_user, patient.patient_id)
    await send(key, adt("A03", "B2"))
    resp = await client.put(f"/api/workflows/{wf_id}/complete",
                            json={"completed_at": utcnow().isoformat()}, headers=physician)
    assert resp.status_code == 400 and "already recorded" in resp.json()["detail"]


# ---------- A02 ----------

async def test_a02_moves_patient_and_encounter(client, make_client, send, db):
    _, key, admin = await make_client()
    unit = (await client.post("/api/units", json={"code": "5E", "name": "5 East"}, headers=admin)).json()
    await send(key, with_pv1(adt("A01", "T1"), f19="V1"))

    ack = await send(key, with_pv1(adt("A02", "T2"), f3="5E^501^A", f19="V1"))
    assert ack["code"] == "AA" and unit["unit_id"] in ack["text"]
    patient = await _patient(db)
    encounter = await db.scalar(select(Encounter))
    assert (patient.room_number, patient.unit_id) == ("501-A", unit["unit_id"])
    assert (encounter.room_number, encounter.unit_id) == ("501-A", unit["unit_id"])
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_A02"))
    assert entry.resource_id == patient.patient_id
    _no_phi(entry)

    assert (await send(key, with_pv1(adt("A02", "T3"), f3="")))["error"] == "101"
    assert (await send(key, with_pv1(adt("A02", "T4"), f3="5E^502^A", f19="V9")))["error"] == "207"
    await send(key, adt("A03", "T5"))
    ack = await send(key, with_pv1(adt("A02", "T6"), f3="5E^503^A"))
    assert (ack["code"], ack["error"], ack["text"]) == ("AE", "207", "Patient is not admitted")


# ---------- A08 ----------

async def test_a08_updates_only_whitelisted_fields(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "U1"))
    message = with_pv1(adt("A08", "U2"), f3="9Z^999^Z").replace("Doe^Jane^Q", "Doe-Smith^Jane^Q") \
        .replace("J18.9^Pneumonia, unspecified organism", "J15.9^Bacterial pneumonia")
    ack = await send(key, message)
    assert (ack["code"], ack["text"]) == ("AA", "Patient updated: name, diagnosis")
    patient = await _patient(db)
    assert patient.name == "Jane Q Doe-Smith" and patient.diagnosis == "Bacterial pneumonia"
    assert patient.room_number == "412-B"  # location only changes with A02

    assert (await send(key, adt("A08", "U3").replace("Doe^Jane^Q", "Doe-Smith^Jane^Q")
                       .replace("J18.9^Pneumonia, unspecified organism", "J15.9^Bacterial pneumonia")))["text"] \
        == "No changes"
    ack = await send(key, adt("A08", "U4").replace("19700101", "19701399"))
    assert (ack["code"], ack["error"]) == ("AE", "102")
    entries = list(await db.scalars(select(AuditLog).where(AuditLog.action == "HL7_ADT_A08")))
    assert entries and all("Doe-Smith" not in e.details and "Bacterial" not in e.details for e in entries)


# ---------- A11 ----------

async def test_a11_cancels_the_admission(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    await send(key, with_pv1(adt("A01", "C1"), f19="V1"))
    ack = await send(key, with_pv1(adt("A11", "C2"), f19="V1"))
    assert (ack["code"], ack["text"]) == ("AA", "Admission cancelled")
    assert (await db.scalar(select(Encounter))).status == "cancelled"
    assert (await _patient(db)).status == "discharged"
    assert (await send(key, adt("A11", "C3")))["text"] == "Admission already cancelled"

    # The corrected admission may reuse the cancelled visit number.
    assert (await send(key, with_pv1(adt("A01", "C4"), f19="V1")))["text"] == "Patient readmitted"
    patient = await _patient(db)
    await _start_discharge(client, make_user, patient.patient_id)
    ack = await send(key, with_pv1(adt("A11", "C5"), f19="V1"))
    assert (ack["code"], ack["error"]) == ("AE", "207") and "discharge workflow" in ack["text"]
    assert await db.scalar(select(Notification).where(Notification.title == "ADT Cancel Admission Not Applied"))
    assert (await _patient(db)).status == "pending_discharge"


# ---------- A13 ----------

async def test_a13_reopens_an_external_discharge(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    await send(key, with_pv1(adt("A01", "D1"), f19="V1"))
    patient = await _patient(db)
    wf_id, physician = await _start_discharge(client, make_user, patient.patient_id)
    assert (await send(key, adt("A13", "D0")))["text"] == "Patient is not discharged; nothing to cancel"
    await send(key, with_pv1(adt("A03", "D2"), f19="V1", f36="01"))

    assert (await send(key, with_pv1(adt("A13", "D3"), f19="V9")))["error"] == "207"
    ack = await send(key, with_pv1(adt("A13", "D4"), f19="V1"))
    assert ack["code"] == "AA" and "back in progress" in ack["text"]
    db.expire_all()
    workflow = await db.get(Workflow, wf_id)
    assert workflow.status == "in_progress" and workflow.departed_at is None and workflow.documentation_due_at is None
    encounter = await db.scalar(select(Encounter))
    assert (encounter.status, encounter.discharged_at, encounter.disposition) == ("active", None, None)
    assert (await _patient(db)).status == "pending_discharge"
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "REOPEN_DISCHARGE"))

    # Ready now: the next A03 completes it normally.
    await _finish_documentation(client, physician, patient.patient_id, wf_id)
    assert (await send(key, adt("A03", "D5")))["text"] == "Discharge completed"


async def test_a13_after_a_discharge_without_workflow(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "E1"))
    await send(key, adt("A03", "E2"))
    assert (await send(key, adt("A13", "E3")))["text"] == "Discharge cancelled; the stay is open again"
    assert (await _patient(db)).status == "admitted"
    assert (await db.scalar(select(Encounter))).status == "active"


# ---------- A40 ----------

def a40(control_id: str, survivor: str, prior: str) -> str:
    return adt("A40", control_id, mrn=survivor) + f"MRG|{prior}^^^GENHOSP^MR\r"


async def test_a40_merges_records_and_retires_the_old_index(client, make_client, make_user, send, db):
    _, key, admin = await make_client()
    # The prior record has a finished stay with a summary; the survivor is in hospital now.
    await send(key, with_pv1(adt("A01", "G1", mrn=OTHER_MRN), f19="V-OLD"))
    prior = await _patient(db, OTHER_MRN)
    wf_id, physician = await _start_discharge(client, make_user, prior.patient_id)
    await _finish_documentation(client, physician, prior.patient_id, wf_id)
    assert (await send(key, adt("A03", "G2", mrn=OTHER_MRN)))["text"] == "Discharge completed"
    await send(key, with_pv1(adt("A01", "G3"), f19="V-NEW"))
    survivor = await _patient(db)
    prior_id, survivor_id = prior.patient_id, survivor.patient_id

    ack = await send(key, a40("G4", MRN, OTHER_MRN))
    assert (ack["code"], ack["text"]) == ("AA", "Records merged")
    db.expire_all()
    prior, survivor = await db.get(Patient, prior_id), await db.get(Patient, survivor_id)
    assert prior.merged_into == survivor_id and prior.mrn_index is None and prior.status == "discharged"
    assert survivor.status == "admitted"
    encounters = {e.visit_number: e for e in await db.scalars(select(Encounter))}
    assert {v: e.patient_id for v, e in encounters.items()} == {"V-OLD": survivor_id, "V-NEW": survivor_id}
    assert (await db.get(Workflow, wf_id)).patient_id == survivor_id
    assert all(t.patient_id == survivor_id for t in await db.scalars(select(Task)))
    summary = await db.scalar(select(DischargeSummary))
    assert summary.patient_id == survivor_id and summary.content and summary.content != "[REDACTED]"
    # Its signed version (ADR-002 item 13) moved too, re-encrypted with the survivor's key: same text, same hash.
    [version] = summary.versions
    assert version.patient_id == survivor_id and version.content == summary.content
    assert version.content_sha256 == content_sha256(version.content)
    assert all(m.patient_id == survivor_id for m in await db.scalars(select(HL7Message)) if m.patient_id)

    listed = [p["patient_id"] for p in (await client.get("/api/patients", headers=admin)).json()]
    assert listed == [survivor_id]
    fhir = (await client.get(f"/api/fhir/R4/Patient/{prior_id}", headers=admin)).json()
    assert fhir["link"] == [{"other": {"reference": f"Patient/{survivor_id}"}, "type": "replaced-by"}]
    entries = list(await db.scalars(select(AuditLog).where(AuditLog.action == "MERGE_PATIENT")))
    assert {e.resource_id for e in entries} == {prior_id, survivor_id}
    for entry in entries:
        _no_phi(entry)
        assert OTHER_MRN not in entry.details

    # The old MRN finds nobody any more, and a resent A40 has nothing left to merge.
    assert (await send(key, adt("A08", "G5", mrn=OTHER_MRN)))["text"].startswith("Parked")
    assert (await send(key, a40("G6", MRN, OTHER_MRN)))["text"] == "No patient with the prior MRN; nothing to merge"


async def test_a40_with_only_the_prior_record_rekeys_it(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "H1", mrn=OTHER_MRN))
    ack = await send(key, a40("H2", MRN, OTHER_MRN))
    assert (ack["code"], ack["text"]) == ("AA", "Patient record now carries the surviving MRN")
    patient = await _patient(db)
    assert patient.mrn == MRN and patient.merged_into is None
    assert (await send(key, adt("A08", "H3", mrn=OTHER_MRN)))["text"].startswith("Parked")
    assert (await send(key, adt("A08", "H4")))["code"] == "AA"


async def test_a40_refuses_two_stays_in_progress(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "J1"))
    await send(key, adt("A01", "J2", mrn=OTHER_MRN))
    ack = await send(key, a40("J3", MRN, OTHER_MRN))
    assert (ack["code"], ack["error"]) == ("AE", "207") and "by hand" in ack["text"]
    assert (await _patient(db, OTHER_MRN)).merged_into is None
    assert await db.scalar(select(Notification).where(Notification.title == "ADT Merge Not Applied"))
    ack = await send(key, adt("A40", "J4"))
    assert (ack["code"], ack["error"]) == ("AE", "100")
    ack = await send(key, a40("J5", MRN, MRN))
    assert (ack["code"], ack["error"]) == ("AE", "102")


# ---------- parking out-of-order messages ----------

async def test_out_of_order_a03_is_parked_encrypted_and_replayed(client, make_client, make_user, send, db):
    created, key, admin = await make_client()
    ack = await send(key, with_pv1(adt("A03", "P1"), f45="20261019090000", f36="01"))
    assert (ack["code"], ack["error"]) == ("AA", None) and ack["text"].startswith("Parked")
    assert await db.scalar(select(Patient)) is None

    raw = (await db.execute(text("SELECT message, mrn_index, status FROM hl7_parked_messages"))).one()
    assert crypto.is_encrypted(raw.message) and "Jane" not in raw.message and MRN not in raw.mrn_index
    assert raw.status == "parked"
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_PARKED"))
    _no_phi(entry)

    listed = (await client.get("/api/hl7/parked", headers=admin)).json()
    assert [(p["event"], p["status"], p["control_id"]) for p in listed] == [("ADT^A03", "parked", "P1")]
    assert not {"message", "dek", "mrn_index"} & set(listed[0])
    _, nurse = await make_user("nurse")
    assert (await client.get("/api/hl7/parked", headers=nurse)).status_code == 403

    # A resend is answered as before and not parked twice.
    assert (await send(key, with_pv1(adt("A03", "P1"), f45="20261019090000")))["text"] == ack["text"]
    assert len(list(await db.scalars(select(HL7ParkedMessage)))) == 1

    ack = await send(key, adt("A01", "P2"))
    assert ack["text"] == "Patient admitted; 1 parked message(s) applied"
    patient = await _patient(db)
    assert patient.status == "discharged"
    encounter = await db.scalar(select(Encounter))
    assert encounter.status == "discharged" and encounter.disposition == "01"
    assert encounter.discharged_at == datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
    parked = await db.scalar(select(HL7ParkedMessage))
    assert (parked.status, parked.message, parked.dek, parked.result_ack) == ("replayed", None, None, "AA")
    original = await db.get(HL7Message, parked.message_id)
    assert original.patient_id == patient.patient_id
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_REPLAYED"))
    assert entry.resource_id == patient.patient_id and parked.parked_id in entry.details
    _no_phi(entry)


async def test_parked_messages_replay_in_arrival_order(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A08", "O1").replace("Doe^Jane^Q", "Roe^Jane^Q"))
    await send(key, adt("A03", "O2"))
    ack = await send(key, adt("A01", "O3"))
    assert ack["text"].endswith("2 parked message(s) applied")
    patient = await _patient(db)
    assert patient.name == "Jane Q Roe" and patient.status == "discharged"


async def test_parked_messages_expire_and_can_be_discarded(client, make_client, make_user, send, db):
    _, key, admin = await make_client()
    await send(key, adt("A03", "X1"))
    await send(key, adt("A02", "X2", mrn=OTHER_MRN))
    await db.execute(update(HL7ParkedMessage).where(HL7ParkedMessage.event == "ADT^A03")
                     .values(expires_at=utcnow() - timedelta(minutes=1)))
    await db.commit()

    listed = {p["event"]: p for p in (await client.get("/api/hl7/parked", headers=admin)).json()}
    assert listed["ADT^A03"]["status"] == "expired" and listed["ADT^A02"]["status"] == "parked"
    db.expire_all()
    expired = await db.scalar(select(HL7ParkedMessage).where(HL7ParkedMessage.event == "ADT^A03"))
    assert expired.message is None and expired.dek is None
    assert await db.scalar(select(Notification).where(Notification.title == "Parked HL7 Messages Expired"))
    # An expired message is not replayed when the admission finally arrives.
    assert (await send(key, adt("A01", "X3")))["text"] == "Patient admitted"
    assert (await _patient(db)).status == "admitted"

    parked_id = listed["ADT^A02"]["parked_id"]
    resp = await client.post(f"/api/hl7/parked/{parked_id}/discard", headers=admin)
    assert resp.status_code == 200 and resp.json()["status"] == "discarded"
    assert (await client.post(f"/api/hl7/parked/{parked_id}/discard", headers=admin)).status_code == 400
    assert (await client.post("/api/hl7/parked/hlp_nope/discard", headers=admin)).status_code == 404
    only_parked = (await client.get("/api/hl7/parked?status=parked", headers=admin)).json()
    assert only_parked == []
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_DISCARDED"))


@requires_postgres
async def test_parked_messages_are_tenant_isolated(client, make_client, make_tenant, make_user, send):
    await make_tenant("hospital_b")
    _, key_a, admin_a = await make_client()
    await send(key_a, adt("A03", "ISO1"))
    _, admin_b = await make_user("admin", tenant_id="hospital_b")
    assert len((await client.get("/api/hl7/parked", headers=admin_a)).json()) == 1
    assert (await client.get("/api/hl7/parked", headers=admin_b)).json() == []
    # Hospital B's A01 for the same MRN does not replay hospital A's parked message.
    _, key_b, _ = await make_client(tenant_id="hospital_b")
    assert (await send(key_b, adt("A01", "ISO2")))["text"] == "Patient admitted"
    assert (await client.get("/api/hl7/parked", headers=admin_a)).json()[0]["status"] == "parked"
