"""Discharge summary text is PHI: encrypted at rest with the patient's data key, shredded with the patient."""
import base64
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.models import DischargeSummary
from app.core import crypto
from app.core.migrate import encrypt_legacy_phi

pytestmark = pytest.mark.anyio

NOTES = "Medications: amoxicillin 500 mg three times daily"
TEXT_COLUMNS = ("content", "original_content", "edit_diff", "structured")


async def _summary(client, make_user, create_patient, edit=True):
    _, physician = await make_user("physician")
    patient = await create_patient(physician, name="Ada Lovelace")
    wf = (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)).json()["workflow"]
    summary = (await client.post("/api/ai/generate-summary", headers=physician, json={
        "patient_id": patient["patient_id"], "workflow_id": wf["workflow_id"], "clinical_notes": NOTES})).json()
    if edit:
        edited = summary["content"] + "\nFollow-up: cardiology in 2 weeks"
        resp = await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician,
                                json={"content": edited})
        assert resp.status_code == 200 and resp.json()["edited"] is True
    return physician, patient, wf, summary


async def _raw(db, summary_id):
    return (await db.execute(text(
        "SELECT content, original_content, edit_diff, structured FROM discharge_summaries WHERE summary_id = :s"
    ), {"s": summary_id})).one()


async def test_summary_text_is_encrypted_at_rest_and_round_trips(client, make_user, create_patient, db):
    physician, patient, wf, summary = await _summary(client, make_user, create_patient)
    assert summary["structured"]["discharge_diagnoses"] and "amoxicillin" in summary["content"]

    raw = await _raw(db, summary["summary_id"])
    assert all(crypto.is_encrypted(v) for v in raw)
    assert not any("amoxicillin" in v or "Ada" in v for v in raw)

    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert got["content"].endswith("cardiology in 2 weeks") and got["original_content"] == summary["content"]
    assert "+Follow-up: cardiology in 2 weeks" in got["edit_diff"]
    assert got["structured"] == summary["structured"]
    detail = (await client.get(f"/api/workflows/{wf['workflow_id']}", headers=physician)).json()
    assert detail["discharge_summary"]["content"] == got["content"]

    doc = (await client.get(f"/api/fhir/R4/DocumentReference/{summary['summary_id']}", headers=physician)).json()
    assert base64.b64decode(doc["content"][0]["attachment"]["data"]).decode("utf-8") == got["content"]

    metrics = (await client.get("/api/ai/metrics", headers=physician)).json()
    assert metrics["summary_quality"][0]["approved"] == 1  # aggregates read plain columns, unaffected


async def test_summary_ciphertext_is_bound_to_its_row(client, make_user, create_patient, db):
    _, _, _, a = await _summary(client, make_user, create_patient, edit=False)
    _, _, _, b = await _summary(client, make_user, create_patient, edit=False)
    sa, sb = await db.get(DischargeSummary, a["summary_id"]), await db.get(DischargeSummary, b["summary_id"])
    sb._content, sb.patient.phi_dek = sa._content, sa.patient.phi_dek  # copy A's ciphertext and key onto B
    with pytest.raises(Exception):
        _ = sb.content  # associated data (summary id) no longer matches


async def test_erasure_redacts_the_summary(client, make_user, create_patient, db):
    physician, patient, wf, summary = await _summary(client, make_user, create_patient)
    _, admin = await make_user("admin")
    assert (await client.delete(f"/api/patients/{patient['patient_id']}/data", headers=admin)).status_code == 200

    assert all(v == "[REDACTED]" for v in await _raw(db, summary["summary_id"]))
    got = (await client.get(f"/api/ai/summary/{wf['workflow_id']}", headers=physician)).json()
    assert got["content"] == got["original_content"] == got["edit_diff"] == "[REDACTED]"
    assert got["structured"] is None
    doc = (await client.get(f"/api/fhir/R4/DocumentReference/{summary['summary_id']}", headers=physician)).json()
    assert base64.b64decode(doc["content"][0]["attachment"]["data"]) == b"[REDACTED]"


async def test_summary_reads_redacted_once_the_key_is_gone(client, make_user, create_patient, db):
    """Crypto-shredding alone (ciphertext left in place) already makes the text unreadable."""
    _, _, _, summary = await _summary(client, make_user, create_patient)
    row = await db.get(DischargeSummary, summary["summary_id"])
    row.patient.shred()
    assert crypto.is_encrypted(row._content)
    assert row.content == row.original_content == row.edit_diff == "[REDACTED]" and row.structured is None


async def test_legacy_plaintext_summaries_are_backfilled(client, make_user, create_patient, engine, db,
                                                          monkeypatch):
    from app.core import migrate

    monkeypatch.setattr(migrate, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    _, _, _, kept = await _summary(client, make_user, create_patient)
    _, erased_patient, _, erased = await _summary(client, make_user, create_patient, edit=False)
    structured = {"discharge_diagnoses": ["Gout"], "medications": []}
    for sid, plain in ((kept["summary_id"], "Legacy text for Ada"), (erased["summary_id"], "Erased text")):
        # Plaintext as written before encryption existed (structured as its JSON text, as migration 0005 leaves it).
        await db.execute(text(
            "UPDATE discharge_summaries SET content = :c, original_content = :c, edit_diff = NULL, structured = :j "
            "WHERE summary_id = :s"), {"c": plain, "j": json.dumps(structured), "s": sid})
    # An erasure that ran before summaries were encrypted left the patient keyless and the summary in plaintext.
    await db.execute(text("UPDATE patients SET phi_dek = NULL, gdpr_erased = true WHERE patient_id = :p"),
                     {"p": erased_patient["patient_id"]})
    await db.commit()

    assert await encrypt_legacy_phi() == 2
    assert await encrypt_legacy_phi() == 0  # idempotent

    raw = await _raw(db, kept["summary_id"])
    assert crypto.is_encrypted(raw.content) and crypto.is_encrypted(raw.structured) and raw.edit_diff is None
    assert tuple(await _raw(db, erased["summary_id"])) == ("[REDACTED]", "[REDACTED]", None, "[REDACTED]")
    db.expire_all()
    row = await db.get(DischargeSummary, kept["summary_id"])
    assert row.content == row.original_content == "Legacy text for Ada" and row.structured == structured
