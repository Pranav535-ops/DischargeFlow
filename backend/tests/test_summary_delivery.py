"""ADR-002 Phase 9, item 16: outbound delivery of the signed discharge summary (FHIR document Bundle or C-CDA)."""
import dataclasses
import json
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.ai.models import DischargeSummary
from app.audit.models import AuditLog
from app.core.db import get_session_factory, utcnow
from app.core.tenancy import current_tenant_id, use_access_scope, use_tenant
from app.delivery import router as delivery_router
from app.delivery import service, transport
from app.delivery.models import SummaryDelivery
from app.delivery.transport import HttpsTransport, MockTransport, Result
from app.main import app
from app.notifications.models import Notification
from tests.conftest import DEFAULT_TENANT, requires_postgres

pytestmark = pytest.mark.anyio

NOTES = "Medications: amoxicillin 500 mg three times daily"
ADDENDUM = "Addendum: potassium 3.1 on the final panel, recheck at follow-up."
CDA = "{urn:hl7-org:v3}"
CRON = "c" * 40
SECRET = "s" * 40


@pytest.fixture
def factory(client, session_factory):
    """Background first attempts and the cron run open their own sessions on the test database."""
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    yield session_factory
    app.dependency_overrides.pop(get_session_factory, None)


@pytest.fixture
def mock():
    m = MockTransport()
    transport.set_transport("mock", m)
    yield m
    transport.set_transport("mock", None)


class FakeReceiver:
    """An httpx transport standing in for the receiving FHIR server."""

    def __init__(self, *statuses: int):
        self.statuses = list(statuses) or [201]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, headers={"Location": "https://pcp.example.test/fhir/Bundle/77"} if status < 300
                              else {}, text="Patient Ada Lovelace not found")  # a body that must never be stored


@pytest.fixture
def receiver():
    def _install(*statuses: int) -> FakeReceiver:
        fake = FakeReceiver(*statuses)
        transport.set_transport("https", HttpsTransport(transport=httpx.MockTransport(fake)))
        return fake
    yield _install
    transport.set_transport("https", None)


async def _endpoint(client, admin, **body):
    resp = await client.post("/api/delivery-endpoints", headers=admin, json={
        "name": "Riverside Family Practice", "kind": "primary_care", "document_format": "fhir_bundle",
        "transport": "mock", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _discharge(client, make_user, create_patient, endpoints=(), physician=None, nurse=None, **patient):
    """A patient with a drafted summary whose stay has the given recipients. Returns ids and headers."""
    if physician is None:
        _, physician = await make_user("physician")
    if nurse is None:
        _, nurse = await make_user("nurse")
    p = await create_patient(physician, name="Ada Lovelace", **patient)
    wf = (await client.post(f"/api/workflows/initiate/{p['patient_id']}", headers=physician)).json()["workflow"]
    for ep in endpoints:
        resp = await client.post(f"/api/encounters/{wf['encounter_id']}/recipients", headers=nurse,
                                 json={"endpoint_id": ep["endpoint_id"]})
        assert resp.status_code == 201, resp.text
        assert resp.json()["queued"] == []  # nothing is signed yet
    summary = (await client.post("/api/ai/generate-summary", headers=physician, json={
        "patient_id": p["patient_id"], "workflow_id": wf["workflow_id"], "clinical_notes": NOTES})).json()
    return {"physician": physician, "nurse": nurse, "patient": p, "workflow": wf, "summary": summary}


async def _sign(client, d, edited_suffix="\nFollow-up: cardiology in 2 weeks"):
    text_ = d["summary"]["content"] + edited_suffix
    resp = await client.put(f"/api/ai/approve-summary/{d['summary']['summary_id']}", headers=d["physician"],
                            json={"content": text_})
    assert resp.status_code == 200, resp.text
    return text_


async def _deliveries(db, **where) -> list[SummaryDelivery]:
    db.expire_all()
    stmt = select(SummaryDelivery).order_by(SummaryDelivery.created_at)
    for k, v in where.items():
        stmt = stmt.where(getattr(SummaryDelivery, k) == v)
    return list(await db.scalars(stmt))


async def _run(session_factory, now=None, tenant_id=DEFAULT_TENANT) -> dict:
    token = current_tenant_id.set(tenant_id)
    try:
        async with session_factory() as session:
            await use_tenant(session, tenant_id)
            result = await service.run_tenant(session, service.system_actor(tenant_id), now)
            await session.commit()
        return result
    finally:
        current_tenant_id.reset(token)


async def _audit(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


def _check_bundle(bundle: dict) -> dict:
    """The FHIR R4 document Bundle rules this code relies on (not a full validator)."""
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "document"
    assert bundle["identifier"]["value"].startswith("urn:uuid:") and bundle["timestamp"]
    entries = bundle["entry"]
    urls = [e["fullUrl"] for e in entries]
    assert all(u.startswith("urn:uuid:") for u in urls) and len(set(urls)) == len(urls)
    composition = entries[0]["resource"]
    assert composition["resourceType"] == "Composition"
    assert composition["type"]["coding"][0] == {"system": "http://loinc.org", "code": "18842-5",
                                                "display": "Discharge summary"}
    for key in ("status", "type", "subject", "date", "author", "title"):
        assert composition.get(key), key

    def refs(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "reference":
                    yield v
                else:
                    yield from refs(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from refs(v)

    for ref in refs(bundle):
        assert ref in urls, f"unresolved reference {ref}"
    for section in composition["section"]:
        assert section["title"] and section["text"]["status"] == "generated"
        div = ET.fromstring(section["text"]["div"])
        assert div.tag == "{http://www.w3.org/1999/xhtml}div"
    by_type = {e["resource"]["resourceType"]: e["resource"] for e in entries}
    assert set(by_type) == {"Composition", "Patient", "Encounter", "Practitioner", "Organization"}
    return by_type


async def test_signing_delivers_fhir_document_bundle(client, make_user, create_patient, db, factory, mock):
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin)
    d = await _discharge(client, make_user, create_patient, [ep])
    signed = await _sign(client, d)

    # Queued in the signing transaction, first attempt right after commit: acknowledged by the receiver.
    [delivery] = await _deliveries(db)
    assert delivery.status == "acknowledged" and delivery.attempts == 1 and delivery.remote_id
    assert delivery.document_format == "fhir_bundle" and delivery.document_sha256
    [sent] = mock.sent
    assert sent.content_type == "application/fhir+json"
    bundle = json.loads(sent.body)
    resources = _check_bundle(bundle)
    composition = resources["Composition"]
    assert composition["status"] == "final" and composition["attester"][0]["mode"] == "legal"
    # The signed (edited) text is what is sent, not the AI draft.
    assert "cardiology in 2 weeks" in composition["section"][0]["text"]["div"]
    assert resources["Patient"]["name"] == [{"text": "Ada Lovelace"}]
    assert resources["Organization"]["name"] == "Default Hospital"
    assert signed.splitlines()[0][:20] in sent.body.decode()

    # Deterministic: the same delivery renders the same document (a retry is recognisable).
    async with factory() as s:
        await use_tenant(s, DEFAULT_TENANT)
        row = await s.get(SummaryDelivery, delivery.delivery_id)
        summary = await s.get(DischargeSummary, row.summary_id)
        endpoint = await service.get_endpoint(s, row.endpoint_id)
        again = await service.build_document(s, row, summary, endpoint)
    assert json.loads(service.documents.render(again, "fhir_bundle")) == bundle

    # Audited without PHI, and visible on the discharge for the care team.
    for action in ("QUEUE_SUMMARY_DELIVERY", "DELIVER_SUMMARY"):
        [entry] = await _audit(db, action)
        assert "Ada" not in entry.details and "amoxicillin" not in entry.details
    assert "acknowledged" in (await _audit(db, "DELIVER_SUMMARY"))[0].details
    listed = (await client.get(f"/api/deliveries?workflow_id={d['workflow']['workflow_id']}",
                               headers=d["physician"])).json()
    assert [x["status"] for x in listed] == ["acknowledged"] and listed[0]["endpoint_name"] == ep["name"]
    assert "Ada" not in json.dumps(listed)


async def test_ccda_document_is_well_formed(client, make_user, create_patient, db, factory, mock):
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, name="St. Mary Skilled Nursing", kind="receiving_facility",
                         document_format="ccda")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    [sent] = mock.sent
    assert sent.content_type == "application/xml" and sent.body.startswith(b"<?xml")
    root = ET.fromstring(sent.body)  # well formed
    assert root.tag == f"{CDA}ClinicalDocument"
    templates = {(t.get("root"), t.get("extension")) for t in root.findall(f"{CDA}templateId")}
    assert ("2.16.840.1.113883.10.20.22.1.1", "2015-08-01") in templates  # US Realm Header
    assert ("2.16.840.1.113883.10.20.22.1.8", "2015-08-01") in templates  # Discharge Summary
    assert root.find(f"{CDA}code").get("code") == "18842-5"
    assert root.find(f"{CDA}typeId").get("extension") == "POCD_HD000040"
    for child in ("id", "title", "effectiveTime", "confidentialityCode", "languageCode", "setId", "versionNumber",
                  "recordTarget", "author", "custodian", "legalAuthenticator", "componentOf", "component"):
        assert root.find(f"{CDA}{child}") is not None, child
    name = root.find(f"{CDA}recordTarget/{CDA}patientRole/{CDA}patient/{CDA}name")
    assert name.find(f"{CDA}given").text == "Ada" and name.find(f"{CDA}family").text == "Lovelace"
    assert root.find(f"{CDA}legalAuthenticator/{CDA}signatureCode").get("code") == "S"
    sections = root.findall(f"{CDA}component/{CDA}structuredBody/{CDA}component/{CDA}section")
    codes = {s.find(f"{CDA}code").get("code"): s for s in sections if s.find(f"{CDA}code") is not None}
    assert {"48765-2", "8648-8", "11535-2", "10183-2", "18776-5"} <= set(codes)
    course = "".join(codes["8648-8"].find(f"{CDA}text").itertext())
    assert "cardiology in 2 weeks" in course
    assert codes["48765-2"].get("nullFlavor") == "NI"  # no structured allergy data: says so
    [delivery] = await _deliveries(db)
    assert delivery.status == "acknowledged" and delivery.document_format == "ccda"


async def test_https_retries_with_backoff_then_fails_onto_worklist(client, make_user, create_patient, db,
                                                                  factory, receiver, monkeypatch):
    monkeypatch.setattr(transport, "settings", dataclasses.replace(transport.settings, delivery_signing_secret=SECRET))
    fake = receiver(503)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir/")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)

    [delivery] = await _deliveries(db)
    assert delivery.status == "queued" and delivery.attempts == 1
    assert delivery.last_error == "http_503" and delivery.last_http_status == 503
    assert delivery.next_attempt_at > utcnow() + timedelta(seconds=30)
    request = fake.requests[0]
    assert str(request.url) == "https://pcp.example.test/fhir/Bundle"
    assert request.headers["content-type"] == "application/fhir+json"
    assert request.headers["idempotency-key"].startswith(f"{DEFAULT_TENANT}:{delivery.delivery_id}:")
    timestamp, signature = [part.split("=", 1)[1] for part in request.headers[transport.SIGNATURE_HEADER].split(",")]
    assert signature == transport.sign(SECRET, timestamp, request.content)

    # Not due yet: a run now sends nothing.
    assert (await _run(factory))["claimed"] == 0
    # Each later run is one more attempt, until the last one gives up.
    now = utcnow()
    for _ in range(service.MAX_ATTEMPTS - 1):
        now += timedelta(days=1)
        await _run(factory, now)
    [delivery] = await _deliveries(db)
    assert delivery.status == "failed" and delivery.attempts == service.MAX_ATTEMPTS
    assert len(fake.requests) == service.MAX_ATTEMPTS
    assert len({r.headers["idempotency-key"] for r in fake.requests}) == 1  # the same document every time
    # The response body (which echoed a name) is never kept.
    assert "Ada" not in json.dumps(delivery.to_dict(), default=str)
    notes = list(await db.scalars(select(Notification).where(Notification.title == service.FAILED_TITLE)))
    assert notes and all("Ada" not in n.message for n in notes)

    # The worklist: case managers (nurses) and admins, not physicians.
    worklist = await client.get("/api/deliveries?status=failed", headers=d["nurse"])
    assert worklist.status_code == 200 and [x["delivery_id"] for x in worklist.json()] == [delivery.delivery_id]
    assert (await client.get("/api/deliveries?status=failed", headers=d["physician"])).status_code == 403
    assert (await client.post(f"/api/deliveries/{delivery.delivery_id}/retry", headers=d["physician"])).status_code \
        == 403

    # Retry: the receiver is back, accepts for processing (202) and acknowledges later via the interface engine.
    fake.statuses = [202]
    resp = await client.post(f"/api/deliveries/{delivery.delivery_id}/retry", headers=d["nurse"])
    assert resp.status_code == 200, resp.text
    [delivery] = await _deliveries(db)
    assert delivery.status == "sent" and delivery.attempts == 1 and delivery.acknowledged_at is None
    key = (await client.post("/api/integrations/clients", json={"name": "Interface engine"},
                             headers=admin)).json()["api_key"]
    assert (await client.post(f"/api/deliveries/{delivery.delivery_id}/acknowledge",
                              headers=d["nurse"])).status_code == 401
    resp = await client.post(f"/api/deliveries/{delivery.delivery_id}/acknowledge", headers={"X-API-Key": key},
                             json={"remote_id": "DocumentReference/991"})
    assert resp.status_code == 200 and resp.json()["status"] == "acknowledged"
    assert (await client.post(f"/api/deliveries/{delivery.delivery_id}/retry", headers=d["nurse"])).status_code == 409


async def test_permanent_refusal_fails_at_once(client, make_user, create_patient, db, factory, receiver):
    receiver(400)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    [delivery] = await _deliveries(db)
    assert delivery.status == "failed" and delivery.attempts == 1 and delivery.last_error == "http_400"
    resp = await client.post(f"/api/deliveries/{delivery.delivery_id}/cancel", headers=d["nurse"])
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert "failed reason=http_400" in (await _audit(db, "DELIVER_SUMMARY"))[0].details


async def test_addendum_sends_amended_document_and_late_recipient_gets_latest(client, make_user, create_patient,
                                                                              db, factory, mock):
    _, admin = await make_user("admin")
    pcp = await _endpoint(client, admin)
    snf = await _endpoint(client, admin, name="St. Mary Skilled Nursing", document_format="ccda")
    d = await _discharge(client, make_user, create_patient, [pcp])
    await _sign(client, d)
    resp = await client.post(f"/api/ai/summaries/{d['summary']['summary_id']}/addenda", headers=d["physician"],
                             json={"content": ADDENDUM})
    assert resp.status_code == 201
    first, amended = await _deliveries(db)
    assert first.trigger_id.startswith("sver_") and amended.trigger_id == resp.json()["addendum_id"]
    assert amended.status == "acknowledged"
    composition = json.loads(mock.sent[1].body)["entry"][0]["resource"]
    assert composition["status"] == "amended"
    assert "potassium 3.1" in composition["section"][-1]["text"]["div"]
    _check_bundle(json.loads(mock.sent[1].body))

    # A recipient added after signature gets the summary as it stands (with the addendum), once.
    resp = await client.post(f"/api/encounters/{d['workflow']['encounter_id']}/recipients", headers=d["nurse"],
                             json={"endpoint_id": snf["endpoint_id"]})
    assert resp.status_code == 201 and len(resp.json()["queued"]) == 1
    root = ET.fromstring(mock.sent[2].body)
    assert root.find(f"{CDA}versionNumber").get("value") == "2"
    assert "potassium 3.1" in "".join(root.itertext())
    again = await client.post(f"/api/encounters/{d['workflow']['encounter_id']}/recipients", headers=d["nurse"],
                              json={"endpoint_id": snf["endpoint_id"]})
    assert again.status_code == 409
    recipients = (await client.get(f"/api/encounters/{d['workflow']['encounter_id']}/recipients",
                                   headers=d["physician"])).json()
    assert [r["endpoint_name"] for r in recipients] == [pcp["name"], snf["name"]]

    # Enqueueing the same trigger twice is a no-op.
    async with factory() as s:
        await use_tenant(s, DEFAULT_TENANT)
        summary = await s.get(DischargeSummary, d["summary"]["summary_id"])
        assert await service.enqueue_for_summary(s, service.system_actor(DEFAULT_TENANT), summary) == []
    assert len(await _deliveries(db)) == 3


async def test_erasure_cancels_open_deliveries(client, make_user, create_patient, db, factory, receiver):
    fake = receiver(503)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    [delivery] = await _deliveries(db)
    assert delivery.status == "queued"
    assert (await client.delete(f"/api/patients/{d['patient']['patient_id']}/data", headers=admin)).status_code == 200
    [delivery] = await _deliveries(db)
    assert delivery.status == "cancelled" and delivery.last_error == "patient_erased"
    await _run(factory, utcnow() + timedelta(days=2))
    assert len(fake.requests) == 1  # nothing more left the hospital
    assert (await client.post(f"/api/deliveries/{delivery.delivery_id}/retry", headers=d["nurse"])).status_code == 409


async def test_runner_cancels_when_patient_erased_after_queueing(client, make_user, create_patient, db, factory,
                                                                 receiver):
    """Belt and braces: even a row erasure missed is re-checked before each send."""
    fake = receiver(503)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    [delivery] = await _deliveries(db)
    await db.execute(text("UPDATE patients SET gdpr_erased = :t"), {"t": True})
    await db.commit()
    assert (await _run(factory, utcnow() + timedelta(days=1)))["cancelled"] == 1
    assert len(fake.requests) == 1


async def test_endpoint_directory_is_admin_managed(client, make_user, monkeypatch):
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    body = {"name": "Clinic", "transport": "https", "url": "https://clinic.example.test/fhir"}
    assert (await client.post("/api/delivery-endpoints", headers=nurse, json=body)).status_code == 403
    ep = (await client.post("/api/delivery-endpoints", headers=admin, json=body)).json()
    assert (await client.post("/api/delivery-endpoints", headers=admin,
                              json={**body, "url": None})).status_code == 400
    listed = (await client.get("/api/delivery-endpoints", headers=nurse)).json()
    assert [e["endpoint_id"] for e in listed] == [ep["endpoint_id"]]
    resp = await client.put(f"/api/delivery-endpoints/{ep['endpoint_id']}", headers=admin, json={"active": False})
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert (await client.get("/api/delivery-endpoints", headers=nurse)).json() == []

    production = dataclasses.replace(service.settings, app_env="production")
    monkeypatch.setattr(service, "settings", production)
    assert (await client.post("/api/delivery-endpoints", headers=admin,
                              json={**body, "transport": "mock"})).status_code == 400
    assert (await client.post("/api/delivery-endpoints", headers=admin,
                              json={**body, "url": "http://clinic.example.test"})).status_code == 400


async def test_inactive_endpoint_fails_delivery(client, make_user, create_patient, db, factory, receiver):
    receiver(503)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    await client.put(f"/api/delivery-endpoints/{ep['endpoint_id']}", headers=admin, json={"active": False})
    await _run(factory, utcnow() + timedelta(days=1))
    [delivery] = await _deliveries(db)
    assert delivery.status == "failed" and delivery.last_error == "endpoint_inactive"


async def test_run_endpoint_authentication(client, make_user, create_patient, db, factory, receiver, monkeypatch):
    fake = receiver(503, 201)
    _, admin = await make_user("admin")
    _, nurse = await make_user("nurse")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    await db.execute(text("UPDATE summary_deliveries SET next_attempt_at = :t"),
                     {"t": utcnow() - timedelta(minutes=1)})
    await db.commit()

    assert (await client.get("/api/deliveries/run")).status_code == 401  # no CRON_SECRET configured
    assert (await client.post("/api/deliveries/run", headers=nurse)).status_code == 403
    monkeypatch.setattr(delivery_router, "settings", dataclasses.replace(delivery_router.settings, cron_secret=CRON))
    assert (await client.get("/api/deliveries/run", headers={"Authorization": "Bearer wrong"})).status_code == 401
    resp = await client.get("/api/deliveries/run", headers={"Authorization": f"Bearer {CRON}"})
    assert resp.status_code == 200, resp.text
    [tenant] = resp.json()["tenants"]
    assert tenant["acknowledged"] == 1 and len(fake.requests) == 2
    resp = await client.post("/api/deliveries/run", headers=admin)
    assert resp.status_code == 200 and resp.json()["tenants"][0]["claimed"] == 0


async def test_transport_failures_are_codes():
    def boom(request):
        raise httpx.ConnectError("could not reach https://secret-token@pcp.example.test")

    result = await HttpsTransport(transport=httpx.MockTransport(boom)).send(transport.Outgoing(
        delivery_id="dlv_x", url="https://pcp.example.test/fhir", document_format="fhir_bundle",
        content_type="application/fhir+json", body=b"{}", idempotency_key="k"))
    assert result == Result(ok=False, error="connect_error")
    assert (await HttpsTransport().send(transport.Outgoing(
        delivery_id="dlv_x", url=None, document_format="ccda", content_type="application/xml", body=b"",
        idempotency_key="k"))).retryable is False


# ---------- Postgres: SKIP LOCKED and RLS ----------

@requires_postgres
async def test_concurrent_runs_skip_claimed_rows(client, make_user, create_patient, db, factory, receiver):
    receiver(503)
    _, admin = await make_user("admin")
    ep = await _endpoint(client, admin, transport="https", url="https://pcp.example.test/fhir")
    d = await _discharge(client, make_user, create_patient, [ep])
    await _sign(client, d)
    later = utcnow() + timedelta(days=1)
    token = current_tenant_id.set(DEFAULT_TENANT)
    try:
        async with factory() as holder:
            await use_tenant(holder, DEFAULT_TENANT)
            locked = (await holder.execute(text("SELECT delivery_id FROM summary_deliveries FOR UPDATE"))).all()
            assert len(locked) == 1
            # While another run holds the row, this run claims nothing instead of waiting or double-sending.
            assert (await _run(factory, later))["claimed"] == 0
            await holder.rollback()
    finally:
        current_tenant_id.reset(token)
    assert (await _run(factory, later))["claimed"] == 1


@requires_postgres
async def test_deliveries_are_tenant_and_unit_scoped(client, make_user, create_patient, session_factory, factory,
                                                     mock):
    _, admin = await make_user("admin")
    units = {}
    for code in ("4W", "ICU"):
        units[code] = (await client.post("/api/units", json={"code": code, "name": code}, headers=admin)).json()["unit_id"]
    physician_doc, physician = await make_user("physician")
    await client.put(f"/api/users/{physician_doc['user_id']}/units", json={"unit_ids": list(units.values())},
                     headers=admin)
    nurse_doc, nurse = await make_user("nurse")
    await client.put(f"/api/users/{nurse_doc['user_id']}/units", json={"unit_ids": list(units.values())},
                     headers=admin)
    ep = await _endpoint(client, admin)
    for code in ("4W", "ICU"):
        d = await _discharge(client, make_user, create_patient, [ep], physician=physician, nurse=nurse,
                             unit_id=units[code])
        await _sign(client, d)

    async def counts(scoped: bool, tenant: str = DEFAULT_TENANT):
        async with session_factory() as s:
            await use_tenant(s, tenant)
            if scoped:
                await use_access_scope(s, nurse_doc["user_id"], [units["4W"]])
            return [await s.scalar(text(f"SELECT count(*) FROM {t}"))
                    for t in ("discharge_recipients", "summary_deliveries", "delivery_endpoints")]

    assert await counts(scoped=False) == [2, 2, 1]
    assert await counts(scoped=True) == [1, 1, 1]
    assert await counts(scoped=False, tenant="other") == [0, 0, 0]
