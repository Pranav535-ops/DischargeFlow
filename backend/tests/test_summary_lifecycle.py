"""ADR-002 Phase 8, item 13: signed summary versions, addenda after signature, and the sign-by deadline."""
import base64
import dataclasses
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.ai import lifecycle
from app.ai.models import DischargeSummary, SignedSummaryError
from app.audit.models import AuditLog
from app.core import crypto
from app.core.config import load_settings
from app.core.db import utcnow
from app.core.migrate import encrypt_legacy_phi
from app.core.tenancy import use_access_scope, use_tenant
from app.discharge.models import Workflow
from app.notifications.models import Notification
from tests.conftest import DEFAULT_TENANT, requires_postgres

pytestmark = pytest.mark.anyio

NOTES = "Medications: amoxicillin 500 mg three times daily"
ADDENDUM = "Addendum: potassium 3.1 on the final panel, recheck at follow-up."


async def _drafted(client, make_user, create_patient, physician=None, **patient):
    if physician is None:
        _, physician = await make_user("physician")
    p = await create_patient(physician, name="Ada Lovelace", **patient)
    wf = (await client.post(f"/api/workflows/initiate/{p['patient_id']}", headers=physician)).json()["workflow"]
    summary = (await client.post("/api/ai/generate-summary", headers=physician, json={
        "patient_id": p["patient_id"], "workflow_id": wf["workflow_id"], "clinical_notes": NOTES})).json()
    return physician, p, wf, summary


async def _signed(client, make_user, create_patient, **patient):
    physician, p, wf, summary = await _drafted(client, make_user, create_patient, **patient)
    edited = summary["content"] + "\nFollow-up: cardiology in 2 weeks"
    resp = await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician,
                            json={"content": edited})
    assert resp.status_code == 200, resp.text
    return physician, p, wf, summary, resp.json(), edited


async def _audit(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


async def test_signing_freezes_version_1(client, make_user, create_patient, db):
    physician, p, wf, summary, signed, edited = await _signed(client, make_user, create_patient)
    assert signed["version"] == 1 and signed["version_id"].startswith("sver_")
    assert signed["content_sha256"] == hashlib.sha256(edited.encode("utf-8")).hexdigest()

    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert got["signed"] is True and got["content"] == edited
    assert got["signed_version"]["content"] == edited and got["signed_version"]["hash_verified"] is True
    assert [v["version"] for v in got["versions"]] == [1] and "content" not in got["versions"][0]
    assert got["addenda"] == [] and got["sign_by"] and got["signed_late"] is False

    # Encrypted at rest with the patient's key, bound to the version row.
    raw = (await db.execute(text("SELECT content FROM discharge_summary_versions"))).scalar_one()
    assert crypto.is_encrypted(raw) and "amoxicillin" not in raw

    # Signed text can't change: not through the API, not through the model.
    again = await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician,
                             json={"content": "something else"})
    assert again.status_code == 400
    row = await db.get(DischargeSummary, summary["summary_id"])
    with pytest.raises(SignedSummaryError):
        row.content = "rewritten after signature"

    [entry] = await _audit(db, "AI_SUMMARY_APPROVED")
    assert entry.details.startswith(f"signed version={signed['version_id']} ")
    assert "amoxicillin" not in entry.details and "Ada" not in entry.details


async def test_addenda_after_signature(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    physician, p, wf, draft = await _drafted(client, make_user, create_patient)
    path = f"/api/ai/summaries/{draft['summary_id']}/addenda"
    assert (await client.post(path, json={"content": ADDENDUM}, headers=physician)).status_code == 400  # unsigned

    await client.put(f"/api/ai/approve-summary/{draft['summary_id']}", headers=physician)
    assert (await client.post(path, json={"content": ADDENDUM}, headers=nurse)).status_code == 403
    assert (await client.post(path, json={"content": ""}, headers=physician)).status_code == 422
    assert (await client.post(path, json={"content": "   "}, headers=physician)).status_code == 422
    assert (await client.post("/api/ai/summaries/sum_missing/addenda", json={"content": ADDENDUM},
                              headers=physician)).status_code == 404

    # Addenda are allowed after the patient has left: that is when late results arrive.
    for task in (await client.get(f"/api/workflows/{wf['workflow_id']}", headers=physician)).json()["tasks"]:
        if task["status"] != "completed":
            _, owner = await make_user(task["assigned_role"])
            await client.put(f"/api/tasks/{task['task_id']}", json={"status": "completed"}, headers=owner)
    assert (await client.put(f"/api/workflows/{wf['workflow_id']}/complete", headers=physician)).status_code == 200

    first = await client.post(path, json={"content": ADDENDUM}, headers=physician)
    assert first.status_code == 201, first.text
    second = await client.post(path, json={"content": "Second addendum."}, headers=physician)
    a1, a2 = first.json(), second.json()
    assert a1["addendum_id"].startswith("add_") and a1["content"] == ADDENDUM
    assert a1["content_sha256"] == hashlib.sha256(ADDENDUM.encode("utf-8")).hexdigest()

    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert [a["addendum_id"] for a in got["addenda"]] == [a1["addendum_id"], a2["addendum_id"]]
    assert {a["version_id"] for a in got["addenda"]} == {got["signed_version"]["version_id"]}
    assert got["content"] == draft["content"]  # the signed text is untouched
    detail = (await client.get(f"/api/workflows/{wf['workflow_id']}", headers=physician)).json()
    assert len(detail["discharge_summary"]["addenda"]) == 2

    raw = list(await db.scalars(text("SELECT content FROM discharge_summary_addenda")))
    assert len(raw) == 2 and all(crypto.is_encrypted(r) and "potassium" not in r for r in raw)

    doc = (await client.get(f"/api/fhir/R4/DocumentReference/{draft['summary_id']}", headers=physician)).json()
    assert doc["docStatus"] == "amended" and len(doc["content"]) == 3
    texts = [base64.b64decode(c["attachment"]["data"]).decode("utf-8") for c in doc["content"]]
    assert texts == [draft["content"], ADDENDUM, "Second addendum."]
    assert doc["content"][0]["attachment"]["title"] == "Discharge summary (version 1)"

    entries = await _audit(db, "SUMMARY_ADDENDUM_SIGNED")
    assert [e.resource_id for e in entries] == [draft["summary_id"]] * 2
    assert entries[0].details == f"addendum={a1['addendum_id']} version={a1['version_id']}"


async def test_erasure_shreds_versions_and_addenda(client, make_user, create_patient, db):
    physician, p, wf, summary, signed, _ = await _signed(client, make_user, create_patient)
    await client.post(f"/api/ai/summaries/{summary['summary_id']}/addenda", json={"content": ADDENDUM},
                      headers=physician)
    _, admin = await make_user("admin")
    assert (await client.delete(f"/api/patients/{p['patient_id']}/data", headers=admin)).status_code == 200

    for table in ("discharge_summary_versions", "discharge_summary_addenda"):
        rows = (await db.execute(text(f"SELECT content, content_sha256 FROM {table}"))).all()
        assert rows and all(r == ("[REDACTED]", None) for r in rows)
    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert got["signed_version"]["content"] == "[REDACTED]" and got["signed_version"]["hash_verified"] is None
    assert got["addenda"][0]["content"] == "[REDACTED]"
    resp = await client.post(f"/api/ai/summaries/{summary['summary_id']}/addenda", json={"content": ADDENDUM},
                             headers=physician)
    assert resp.status_code == 400


async def _age_workflow(db, workflow_id, hours):
    """Move the expected discharge time `hours` into the past."""
    await db.execute(update(Workflow).where(Workflow.workflow_id == workflow_id)
                     .values(estimated_discharge_time=utcnow() - timedelta(hours=hours)))
    await db.commit()


async def test_sign_by_deadline_overdue_and_notifications(client, make_user, create_patient, db):
    physician_doc, physician = await make_user("physician")
    _, p, late_wf, late = await _drafted(client, make_user, create_patient, physician=physician)
    _, _, fresh_wf, _ = await _drafted(client, make_user, create_patient, physician=physician)
    await _age_workflow(db, late_wf["workflow_id"], 30)  # 24 h policy: 6 h overdue

    detail = (await client.get(f"/api/workflows/{late_wf['workflow_id']}", headers=physician)).json()
    assert detail["summary_overdue"] is True and detail["summary_sign_by"]
    fresh = (await client.get(f"/api/workflows/{fresh_wf['workflow_id']}", headers=physician)).json()
    assert fresh["summary_overdue"] is False

    overdue = (await client.get("/api/ai/summaries/overdue", headers=physician)).json()
    assert overdue["sign_by_hours"] == 24
    [row] = overdue["overdue"]
    assert row["workflow_id"] == late_wf["workflow_id"] and row["summary_id"] == late["summary_id"]
    assert 5.9 <= row["hours_overdue"] <= 6.1 and set(row) == {
        "workflow_id", "patient_id", "unit_id", "summary_id", "workflow_status", "sign_by", "hours_overdue"}
    _, nurse = await make_user("nurse")
    assert (await client.get("/api/ai/summaries/overdue", headers=nurse)).status_code == 403

    ops = (await client.get("/api/analytics/operations", headers=(await make_user("admin"))[1])).json()
    assert ops["summary_signing"]["overdue_unsigned"] == 1 and ops["summary_signing"]["sign_by_hours"] == 24

    # Notified once per discharge, however often the sweep runs.
    assert (await client.post("/api/ai/summaries/overdue/notify", headers=physician)).json() == {"notified": 1}
    assert (await client.post("/api/ai/summaries/overdue/notify", headers=physician)).json() == {"notified": 0}
    notes = list(await db.scalars(select(Notification).where(Notification.title == lifecycle.OVERDUE_TITLE)))
    assert {n.user_id for n in notes} == {physician_doc["user_id"]}
    assert notes[0].link == f"/workflows/{late_wf['workflow_id']}" and "Ada" not in notes[0].message

    # Signing late clears it and counts as late.
    await client.put(f"/api/ai/approve-summary/{late['summary_id']}", headers=physician)
    assert (await client.get("/api/ai/summaries/overdue", headers=physician)).json()["overdue"] == []
    got = (await client.get(f"/api/ai/summary/{late_wf['workflow_id']}", headers=physician)).json()
    assert got["signed_late"] is True
    ops = (await client.get("/api/analytics/operations", headers=(await make_user("admin"))[1])).json()
    assert ops["summary_signing"]["overdue_unsigned"] == 0
    assert ops["summary_signing"]["signed"] == {"n": 1, "breached": 1}


async def test_missing_summary_is_overdue_too(client, make_user, create_patient, db, session_factory):
    _, physician = await make_user("physician")
    p = await create_patient(physician)
    wf = (await client.post(f"/api/workflows/initiate/{p['patient_id']}", headers=physician)).json()["workflow"]
    await _age_workflow(db, wf["workflow_id"], 25)
    [row] = (await client.get("/api/ai/summaries/overdue", headers=physician)).json()["overdue"]
    assert row["summary_id"] is None

    # The sweep as `app.cli summary-deadlines` runs it: its own session, outside any request.
    async with session_factory() as s:
        await use_tenant(s, DEFAULT_TENANT)
        assert await lifecycle.notify_overdue(s) == 1
        await s.commit()


def test_sign_hours_setting(monkeypatch):
    monkeypatch.setenv("SUMMARY_SIGN_HOURS", "48")
    assert load_settings().summary_sign_hours == 48
    for bad in ("0", "abc", "100000"):
        monkeypatch.setenv("SUMMARY_SIGN_HOURS", bad)
        with pytest.raises(RuntimeError, match="SUMMARY_SIGN_HOURS"):
            load_settings()


async def test_policy_hours_move_the_deadline(client, make_user, create_patient, db, monkeypatch):
    _, physician = await make_user("physician")
    _, _, wf, _ = await _drafted(client, make_user, create_patient, physician=physician)
    await _age_workflow(db, wf["workflow_id"], 30)
    monkeypatch.setattr(lifecycle, "settings", dataclasses.replace(lifecycle.settings, summary_sign_hours=48))
    assert (await client.get("/api/ai/summaries/overdue", headers=physician)).json()["overdue"] == []


async def test_backfill_gives_legacy_signed_summaries_version_1(client, make_user, create_patient, db,
                                                               session_factory, monkeypatch):
    from app.core import migrate

    monkeypatch.setattr(migrate, "SessionLocal", session_factory)
    physician, p, wf, summary, signed, edited = await _signed(client, make_user, create_patient)
    await db.execute(text("DELETE FROM discharge_summary_versions"))  # as if signed before migration 0013
    await db.commit()
    assert await encrypt_legacy_phi() >= 1
    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert got["signed_version"]["version"] == 1 and got["signed_version"]["content"] == edited
    assert got["signed_version"]["hash_verified"] is True
    assert await encrypt_legacy_phi() == 0  # idempotent


# ---------- RLS (Postgres) ----------

@requires_postgres
async def test_versions_and_addenda_are_unit_scoped(client, make_user, create_patient, session_factory):
    _, admin = await make_user("admin")
    units = {}
    for code in ("4W", "ICU"):
        units[code] = (await client.post("/api/units", json={"code": code, "name": code}, headers=admin)).json()["unit_id"]
    physician_doc, physician = await make_user("physician")
    await client.put(f"/api/users/{physician_doc['user_id']}/units", json={"unit_ids": list(units.values())},
                     headers=admin)
    nurse_doc, _ = await make_user("nurse")
    for code in ("4W", "ICU"):
        _, _, _, s = await _drafted(client, make_user, create_patient, physician=physician, unit_id=units[code])
        await client.put(f"/api/ai/approve-summary/{s['summary_id']}", headers=physician)
        await client.post(f"/api/ai/summaries/{s['summary_id']}/addenda", json={"content": ADDENDUM},
                          headers=physician)

    async def counts(scoped: bool, tenant: str = DEFAULT_TENANT):
        async with session_factory() as s:
            await use_tenant(s, tenant)
            if scoped:
                await use_access_scope(s, nurse_doc["user_id"], [units["4W"]])
            return [await s.scalar(text(f"SELECT count(*) FROM {t}"))
                    for t in ("discharge_summary_versions", "discharge_summary_addenda")]

    assert await counts(scoped=False) == [2, 2]
    assert await counts(scoped=True) == [1, 1]
    assert await counts(scoped=False, tenant="other") == [0, 0]
