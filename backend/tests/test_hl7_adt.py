"""Inbound HL7 v2 ADT (ADR-001 Phase 6, item 24): API-key clients, MRN blind index, A01/A03 and their ACKs."""
import pytest
from sqlalchemy import delete, select, text

from app.audit.models import AuditLog
from app.core import crypto
from app.discharge.models import Workflow
from app.identity import permissions
from app.identity.models import RolePermission
from app.interop import hl7
from app.interop.hl7_router import HL7_MEDIA_TYPE
from app.interop.models import HL7Message
from app.notifications.models import Notification
from app.patients.models import Patient
from tests.conftest import requires_postgres

pytestmark = pytest.mark.anyio

MRN = "MRN0042"
# PV1-3 location (unit^room^bed), PV1-7 attending (id^family^given^middle^suffix^prefix), PV1-44 admit time.
PV1 = ["PV1", "1", "I", "4W^412^B"] + [""] * 41
PV1[7], PV1[44] = "^House^Gregory^^^Dr.", "20261017083000"


def adt(event: str, control_id: str, *, mrn: str = MRN, msg_type: str = "ADT", processing: str = "P",
        extra: str = "") -> str:
    segments = [
        f"MSH|^~\\&|EPIC|GENHOSP|DISCHARGEFLOW|GENHOSP|20261018120000||{msg_type}^{event}^ADT_A01|{control_id}|"
        f"{processing}|2.5.1",
        f"EVN|{event}|20261018115500",
        f"PID|1||{mrn}^^^GENHOSP^MR~999-99-9999^^^SSA^SS||Doe^Jane^Q||19700101|F",
        "|".join(PV1),
        "DG1|1||J18.9^Pneumonia, unspecified organism^I10",
    ]
    return "\r".join(segments) + "\r" + extra


def ack_fields(body: str) -> dict:
    segs = {line.split("|")[0]: line.split("|") for line in body.split("\r") if line}
    msa, err = segs["MSA"], segs.get("ERR")
    return {"code": msa[1], "control_id": msa[2], "text": hl7.Encoding().unescape(msa[3]),
            "error": err[3].split("^")[0] if err else None, "msh": segs["MSH"]}


@pytest.fixture
def make_client(client, make_user):
    async def _make(name: str = "Interface engine", tenant_id: str = "default"):
        _, admin = await make_user("admin", tenant_id=tenant_id)
        resp = await client.post("/api/integrations/clients", json={"name": name}, headers=admin)
        assert resp.status_code == 200, resp.text
        return resp.json(), {"X-API-Key": resp.json()["api_key"]}, admin

    return _make


@pytest.fixture
def send(client):
    async def _send(key_headers: dict, message: str):
        resp = await client.post("/api/hl7/adt", content=message.encode(),
                                 headers={**key_headers, "Content-Type": HL7_MEDIA_TYPE})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("x-application/hl7-v2+er7")
        return ack_fields(resp.text)

    return _send


# ---------- parser ----------

def test_parser_numbers_msh_fields_and_unescapes():
    message = hl7.parse("\x0b" + adt("A01", "C1").replace("Doe^Jane^Q", "O\\S\\Brien\\T\\Co^Jane") + "\x1c\r")
    assert (message.message_type, message.trigger_event, message.control_id, message.processing_id) == \
           ("ADT", "A01", "C1", "P")
    assert message.msh.component(3) == "EPIC" and message.version == "2.5.1"
    pid = message.segment("PID")
    assert pid.component(5, 1) == "O^Brien&Co"
    assert pid.component(3, 1, rep=1) == "999-99-9999" and pid.component(3, 5, rep=1) == "SS"


@pytest.mark.parametrize("raw", ["", "PID|1||X", "MSH", "MSHa^~\\&|x"])
def test_parser_rejects_messages_without_a_usable_msh(raw):
    with pytest.raises(hl7.HL7Error):
        hl7.parse(raw)


def test_ack_escapes_text_and_answers_the_sender():
    ack = hl7.build_ack(hl7.parse(adt("A03", "C9")), "AE", "3 tasks | still ^ pending", "207")
    fields = ack_fields(ack)
    assert fields["code"] == "AE" and fields["control_id"] == "C9" and fields["error"] == "207"
    assert fields["text"] == "3 tasks | still ^ pending"
    assert fields["msh"][4:6] == ["EPIC", "GENHOSP"] and fields["msh"][8] == "ACK^A03^ACK"


def test_mrn_blind_index_depends_on_tenant_and_authority():
    base = crypto.blind_index("mrn", "default", "GENHOSP", MRN)
    assert len(base) == 64 and MRN not in base
    assert base != crypto.blind_index("mrn", "hospital_b", "GENHOSP", MRN)
    assert base != crypto.blind_index("mrn", "default", "OTHER", MRN)
    assert base in crypto.blind_index_candidates("mrn", "default", "GENHOSP", MRN)


# ---------- integration clients ----------

async def test_admin_manages_integration_clients(client, make_client, make_user, db):
    created, key, admin = await make_client()
    assert created["api_key"].startswith(f"default.{created['client_id']}.")
    assert "secret_hash" not in created

    listed = (await client.get("/api/integrations/clients", headers=admin)).json()
    assert [c["client_id"] for c in listed] == [created["client_id"]] and "api_key" not in listed[0]
    assert "secret_hash" not in listed[0]

    _, nurse = await make_user("nurse")
    assert (await client.post("/api/integrations/clients", json={"name": "x"}, headers=nurse)).status_code == 403

    resp = await client.delete(f"/api/integrations/clients/{created['client_id']}", headers=admin)
    assert resp.status_code == 200 and resp.json()["revoked_at"]
    assert (await client.post("/api/hl7/adt", content=adt("A01", "R1"), headers=key)).status_code == 401

    actions = set(await db.scalars(select(AuditLog.action)))
    assert {"CREATE_INTEGRATION_CLIENT", "REVOKE_INTEGRATION_CLIENT"} <= actions


async def test_adt_endpoint_requires_a_valid_key_and_permission(client, make_client, make_user, db):
    created, key, _ = await make_client()
    _, physician = await make_user("physician")
    assert (await client.post("/api/hl7/adt", content=adt("A01", "K1"))).status_code == 401
    # A person's bearer token is not an integration credential.
    assert (await client.post("/api/hl7/adt", content=adt("A01", "K1"), headers=physician)).status_code == 401
    bad = {"X-API-Key": key["X-API-Key"][:-2] + "xx"}
    assert (await client.post("/api/hl7/adt", content=adt("A01", "K1"), headers=bad)).status_code == 401
    wrong_tenant = {"X-API-Key": "hospital_b" + key["X-API-Key"][len("default"):]}
    assert (await client.post("/api/hl7/adt", content=adt("A01", "K1"), headers=wrong_tenant)).status_code == 401

    await db.execute(delete(RolePermission).where(RolePermission.role == "interface"))
    await db.commit()
    permissions.clear_cache()
    assert (await client.post("/api/hl7/adt", content=adt("A01", "K1"), headers=key)).status_code == 403
    assert (await db.scalar(select(Patient))) is None


# ---------- A01 ----------

async def test_a01_admits_patient_with_encrypted_mrn_and_audits(make_client, send, db):
    created, key, _ = await make_client()
    ack = await send(key, adt("A01", "MSG001"))
    assert (ack["code"], ack["control_id"], ack["text"]) == ("AA", "MSG001", "Patient admitted")

    patient = await db.scalar(select(Patient))
    assert patient.name == "Jane Q Doe" and patient.date_of_birth == "1970-01-01"
    assert patient.mrn == MRN and patient.mrn_authority == "GENHOSP"
    assert patient.room_number == "412-B" and patient.attending_physician == "Dr. Gregory House"
    assert patient.admission_date == "2026-10-17" and patient.diagnosis == "Pneumonia, unspecified organism"
    assert patient.status == "admitted"
    raw = (await db.execute(text("SELECT mrn, mrn_index FROM patients"))).one()
    assert crypto.is_encrypted(raw.mrn) and MRN not in raw.mrn_index
    assert "mrn_index" not in patient.to_dict() and patient.to_dict()["mrn"] == MRN

    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_A01"))
    assert entry.user_id == created["client_id"] and entry.resource_id == patient.patient_id
    assert "Jane" not in entry.details and MRN not in entry.details

    record = await db.scalar(select(HL7Message))
    assert (record.control_id, record.ack_code, record.patient_id) == ("MSG001", "AA", patient.patient_id)


async def test_resent_message_replays_its_ack_without_reapplying(make_client, send, db):
    _, key, _ = await make_client()
    first = await send(key, adt("A01", "DUP1"))
    again = await send(key, adt("A01", "DUP1"))
    assert first["code"] == again["code"] == "AA" and again["text"] == "Patient admitted"
    assert len(list(await db.scalars(select(Patient)))) == 1
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_REPLAY"))


async def test_a01_for_admitted_mrn_is_ae(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, adt("A01", "A1"))
    ack = await send(key, adt("A01", "A2"))
    assert (ack["code"], ack["error"]) == ("AE", "205")
    assert len(list(await db.scalars(select(Patient)))) == 1


async def test_a01_missing_required_fields_is_ae(make_client, send, db):
    _, key, _ = await make_client()
    ack = await send(key, adt("A01", "N1").replace(f"{MRN}^^^GENHOSP^MR~999-99-9999^^^SSA^SS", ""))
    assert (ack["code"], ack["error"]) == ("AE", "101") and "PID-3" in ack["text"]
    ack = await send(key, adt("A01", "N2").replace("19700101", "19701399"))
    assert (ack["code"], ack["error"]) == ("AE", "102")
    assert await db.scalar(select(Patient)) is None


@pytest.mark.parametrize("kwargs,code", [
    ({"event": "A08"}, "201"),
    ({"msg_type": "ORU"}, "200"),
    ({"processing": "T"}, "202"),
])
async def test_unsupported_messages_are_rejected(make_client, send, db, kwargs, code):
    _, key, _ = await make_client()
    event = kwargs.pop("event", "A01")
    ack = await send(key, adt(event, "U1", **kwargs))
    assert (ack["code"], ack["error"]) == ("AR", code)
    assert await db.scalar(select(Patient)) is None
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_REJECTED"))


async def test_unparseable_message_gets_ar(make_client, send):
    _, key, _ = await make_client()
    ack = await send(key, "this is not hl7")
    assert (ack["code"], ack["error"]) == ("AR", "100")
    ack = await send(key, adt("A01", ""))
    assert (ack["code"], ack["error"]) == ("AR", "101")


# ---------- A03 ----------

async def _admit_and_start_discharge(client, make_user, send, key) -> tuple[str, str, dict]:
    await send(key, adt("A01", "ADM1"))
    _, physician = await make_user("physician")
    pid = (await client.get("/api/patients", headers=physician)).json()[0]["patient_id"]
    resp = await client.post(f"/api/workflows/initiate/{pid}", headers=physician)
    return pid, resp.json()["workflow"]["workflow_id"], physician


async def test_a03_refused_by_state_machine_is_ae_and_notifies(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    pid, wf_id, physician = await _admit_and_start_discharge(client, make_user, send, key)

    ack = await send(key, adt("A03", "DIS1"))
    assert (ack["code"], ack["error"], ack["text"]) == ("AE", "207", "8 tasks still pending")
    assert (await db.get(Workflow, wf_id)).status == "in_progress"
    notes = (await client.get("/api/notifications", headers=physician)).json()
    note = next(n for n in notes if n["title"] == "ADT Discharge Not Applied")
    assert "8 tasks still pending" in note["message"] and note["link"] == f"/workflows/{wf_id}"


async def test_a03_completes_ready_workflow_then_a01_readmits(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    pid, wf_id, physician = await _admit_and_start_discharge(client, make_user, send, key)
    summary = (await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf_id},
                                 headers=physician)).json()
    assert (await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)).status_code == 200
    for task in (await client.get(f"/api/workflows/{wf_id}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed", "reason": "Physician override in test"}, headers=physician)

    ack = await send(key, adt("A03", "DIS2"))
    assert (ack["code"], ack["text"]) == ("AA", "Discharge completed")
    db.expire_all()
    assert (await db.get(Workflow, wf_id)).status == "completed"
    assert (await db.get(Patient, pid)).status == "discharged"
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "HL7_ADT_A03", AuditLog.resource_id == pid))

    # A second A03 (a new message, not a resend) changes nothing and is accepted.
    assert (await send(key, adt("A03", "DIS3")))["text"] == "Patient already discharged"

    ack = await send(key, adt("A01", "ADM2"))
    assert (ack["code"], ack["text"]) == ("AA", "Patient readmitted")
    db.expire_all()
    assert (await db.get(Patient, pid)).status == "admitted"
    assert len(list(await db.scalars(select(Patient)))) == 1


async def test_a03_unknown_mrn_or_no_workflow_is_ae(client, make_client, make_user, send, db):
    _, key, _ = await make_client()
    assert (await send(key, adt("A03", "X1", mrn="NOPE")))["error"] == "204"

    await send(key, adt("A01", "X2"))
    await make_user("physician")
    ack = await send(key, adt("A03", "X3"))
    assert (ack["code"], ack["text"]) == ("AE", "No discharge workflow has been started")
    assert await db.scalar(select(Notification).where(Notification.title == "ADT Discharge Not Applied"))


async def test_erasure_drops_the_mrn_index(client, make_client, send, db):
    _, key, admin = await make_client()
    await send(key, adt("A01", "E1"))
    pid = (await db.scalar(select(Patient))).patient_id
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200

    db.expire_all()
    erased = await db.get(Patient, pid)
    assert erased.mrn_index is None and erased.mrn_authority is None and erased.mrn == "[REDACTED]"
    assert (await send(key, adt("A03", "E2")))["error"] == "204"


# ---------- tenant isolation ----------

async def test_same_mrn_in_two_tenants_are_different_patients(make_client, make_tenant, send, db):
    await make_tenant("hospital_b")
    _, key_a, _ = await make_client()
    _, key_b, _ = await make_client(tenant_id="hospital_b")
    assert (await send(key_a, adt("A01", "T1")))["code"] == "AA"
    assert (await send(key_b, adt("A01", "T1")))["code"] == "AA"  # control ids are per client, MRNs per tenant
    rows = (await db.execute(text("SELECT tenant_id, mrn_index FROM patients"))).all()
    assert {r.tenant_id for r in rows} == {"default", "hospital_b"} and rows[0].mrn_index != rows[1].mrn_index


@requires_postgres
async def test_integration_tables_are_tenant_isolated(client, make_client, make_tenant, make_user, send):
    await make_tenant("hospital_b")
    _, key_a, _ = await make_client()
    await send(key_a, adt("A01", "ISO1"))
    _, admin_b = await make_user("admin", tenant_id="hospital_b")
    assert (await client.get("/api/integrations/clients", headers=admin_b)).json() == []
    assert (await client.get("/api/patients", headers=admin_b)).json() == []
