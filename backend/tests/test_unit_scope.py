"""ADR-002 Phase 7, item 3: units, unit-scoped patient access (Postgres RLS) and break-glass."""
import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.audit.models import AuditLog
from app.core import security
from app.core.db import utcnow
from app.notifications.models import Notification
from app.patients.models import REDACTED, AccessGrant
from tests.conftest import requires_postgres

pytestmark = pytest.mark.anyio


@pytest.fixture
def scoping(monkeypatch):
    """Turn UNIT_SCOPED_ACCESS on (identities are computed per session, and every test makes fresh sessions)."""
    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, unit_scoped_access=True))


@pytest.fixture
def ward(client, make_user):
    """Two units, an admin, and a helper that makes a user who is a member of some units."""
    state = {}

    async def setup():
        _, admin = await make_user("admin")
        units = {}
        for code in ("4W", "ICU"):
            resp = await client.post("/api/units", json={"code": code, "name": f"Unit {code}"}, headers=admin)
            assert resp.status_code == 200, resp.text
            units[code] = resp.json()["unit_id"]
        state.update(admin=admin, units=units)

        async def member(role: str, *codes: str):
            doc, headers = await make_user(role)
            resp = await client.put(f"/api/users/{doc['user_id']}/units",
                                    json={"unit_ids": [units[c] for c in codes]}, headers=admin)
            assert resp.status_code == 200, resp.text
            return doc, headers

        state["member"] = member
        return state

    return setup


async def audit_rows(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


# ---------- units and memberships (both databases) ----------

async def test_units_and_memberships(client, make_user, db, ward):
    w = await ward()
    admin, units = w["admin"], w["units"]
    nurse_doc, nurse = await make_user("nurse")

    assert [u["code"] for u in (await client.get("/api/units", headers=nurse)).json()] == ["4W", "ICU"]
    assert (await client.post("/api/units", json={"code": "5E", "name": "5 East"}, headers=nurse)).status_code == 403
    assert (await client.post("/api/units", json={"code": "4w", "name": "dup"}, headers=admin)).status_code == 409
    assert (await client.post("/api/units", json={"code": "4 W", "name": "x"}, headers=admin)).status_code == 422
    assert [e.details for e in await audit_rows(db, "CREATE_UNIT")] == ["4W", "ICU"]

    path = f"/api/users/{nurse_doc['user_id']}/units"
    assert (await client.put(path, json={"unit_ids": [units["4W"]]}, headers=nurse)).status_code == 403
    assert (await client.put(path, json={"unit_ids": ["unit_nope"]}, headers=admin)).status_code == 400
    resp = await client.put(path, json={"unit_ids": [units["ICU"], units["4W"], units["4W"]]}, headers=admin)
    assert resp.status_code == 200 and resp.json()["unit_ids"] == sorted([units["4W"], units["ICU"]])
    # Takes effect at once (identity cache invalidated), and replaces rather than adds.
    assert (await client.get("/api/auth/me", headers=nurse)).json()["unit_ids"] == sorted(units.values())
    assert (await client.put(path, json={"unit_ids": [units["ICU"]]}, headers=admin)).status_code == 200
    assert (await client.get("/api/auth/me", headers=nurse)).json()["unit_ids"] == [units["ICU"]]
    listed = {u["user_id"]: u["unit_ids"] for u in (await client.get("/api/users", headers=admin)).json()}
    assert listed[nurse_doc["user_id"]] == [units["ICU"]]
    assert [e.details for e in await audit_rows(db, "UPDATE_USER_UNITS")] == ["units: 4W, ICU", "units: ICU"]


async def test_patient_unit_is_validated(client, create_patient, ward):
    w = await ward()
    admin, units = w["admin"], w["units"]
    resp = await client.post("/api/patients", json={
        "name": "X", "date_of_birth": "1970-01-01", "admission_date": "2026-10-01", "room_number": "1",
        "diagnosis": "d", "attending_physician": "Dr", "unit_id": "unit_nope"}, headers=admin)
    assert resp.status_code == 400
    patient = await create_patient(admin, unit_id=units["4W"])
    assert patient["unit_id"] == units["4W"]
    resp = await client.put(f"/api/patients/{patient['patient_id']}", json={"unit_id": units["ICU"]}, headers=admin)
    assert resp.json()["unit_id"] == units["ICU"]
    assert (await client.put(f"/api/patients/{patient['patient_id']}", json={"unit_id": "unit_nope"},
                             headers=admin)).status_code == 400


async def test_who_is_unit_scoped(client, make_user, scoping):
    _, nurse = await make_user("nurse")
    _, physician = await make_user("physician")
    _, admin = await make_user("admin")
    assert (await client.get("/api/auth/me", headers=nurse)).json()["unit_scoped"] is True
    assert (await client.get("/api/auth/me", headers=physician)).json()["unit_scoped"] is True
    assert (await client.get("/api/auth/me", headers=admin)).json()["unit_scoped"] is False  # patient:read_all


async def test_nobody_is_scoped_with_the_flag_off(client, make_user):
    _, nurse = await make_user("nurse")
    assert (await client.get("/api/auth/me", headers=nurse)).json()["unit_scoped"] is False


# ---------- break-glass (both databases) ----------

async def test_break_glass_grant_is_audited_reviewed_and_notified(client, create_patient, db, ward, scoping):
    w = await ward()
    admin, units = w["admin"], w["units"]
    nurse_doc, nurse = await w["member"]("nurse", "4W")
    icu_patient = (await create_patient(admin, unit_id=units["ICU"]))["patient_id"]
    own_patient = (await create_patient(admin, unit_id=units["4W"]))["patient_id"]
    reason = "Covering ICU during the code on bed 12"

    assert (await client.post(f"/api/patients/{icu_patient}/break-glass", json={"reason": reason},
                              headers=admin)).status_code == 400  # admins are not unit-scoped
    assert (await client.post(f"/api/patients/{icu_patient}/break-glass", json={"reason": "short"},
                              headers=nurse)).status_code == 422
    assert (await client.post(f"/api/patients/{own_patient}/break-glass", json={"reason": reason},
                              headers=nurse)).status_code == 409
    assert (await client.post("/api/patients/pat_missing/break-glass", json={"reason": reason},
                              headers=nurse)).status_code == 404

    resp = await client.post(f"/api/patients/{icu_patient}/break-glass", json={"reason": reason}, headers=nurse)
    assert resp.status_code == 200, resp.text
    grant = resp.json()
    assert grant["user_id"] == nurse_doc["user_id"] and grant["patient_id"] == icu_patient
    # A second request while the grant is active is not needed.
    assert (await client.post(f"/api/patients/{icu_patient}/break-glass", json={"reason": reason},
                              headers=nurse)).status_code == 409

    [entry] = await audit_rows(db, "BREAK_GLASS")
    assert entry.resource_id == icu_patient and grant["grant_id"] in entry.details and reason not in entry.details
    notes = (await client.get("/api/notifications", headers=admin)).json()
    assert any(n["title"] == "Break-Glass Access" and grant["grant_id"] in n["message"] for n in notes)

    assert (await client.get("/api/access-grants", headers=nurse)).status_code == 403
    [listed] = (await client.get("/api/access-grants", headers=admin)).json()
    assert listed["reason"] == reason and listed["user_name"] == "Test nurse" and listed["active"] is True

    await db.execute(update(AccessGrant).values(expires_at=utcnow() - timedelta(minutes=1)))
    await db.commit()
    assert (await client.get("/api/access-grants?active_only=true", headers=admin)).json() == []
    assert (await client.get("/api/access-grants", headers=admin)).json()[0]["active"] is False

    # Erasure redacts the free-text reason (it may name the patient).
    assert (await client.delete(f"/api/patients/{icu_patient}/data", headers=admin)).status_code == 200
    assert (await client.get("/api/access-grants", headers=admin)).json()[0]["reason"] == REDACTED


async def test_notifications_go_to_the_patients_unit(client, create_patient, db, ward, scoping):
    w = await ward()
    admin, units = w["admin"], w["units"]
    west_doc, _ = await w["member"]("nurse", "4W")
    icu_doc, _ = await w["member"]("nurse", "ICU")
    _, physician = await w["member"]("physician", "4W", "ICU")

    async def told(user_id):
        return [n.title for n in await db.scalars(select(Notification).where(Notification.user_id == user_id))]

    on_west = (await create_patient(admin, unit_id=units["4W"]))["patient_id"]
    assert (await client.post(f"/api/workflows/initiate/{on_west}", headers=physician)).status_code == 200
    assert "New Discharge Initiated" in await told(west_doc["user_id"])
    assert "New Discharge Initiated" not in await told(icu_doc["user_id"])

    unassigned = (await create_patient(admin))["patient_id"]
    assert (await client.post(f"/api/workflows/initiate/{unassigned}", headers=physician)).status_code == 200
    assert (await told(icu_doc["user_id"])).count("New Discharge Initiated") == 1


# ---------- RLS enforcement (Postgres) ----------

@requires_postgres
async def test_rls_limits_scoped_users_to_their_units(client, create_patient, db, ward, scoping):
    w = await ward()
    admin, units = w["admin"], w["units"]
    _, nurse = await w["member"]("nurse", "4W")
    _, physician = await make_physician(w)
    west = (await create_patient(admin, name="West Patient", unit_id=units["4W"]))["patient_id"]
    icu = (await create_patient(admin, name="Icu Patient", unit_id=units["ICU"]))["patient_id"]
    pool = (await create_patient(admin, name="Pool Patient"))["patient_id"]
    wf = {}
    for pid in (west, icu):
        wf[pid] = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]["workflow_id"]
        await client.post("/api/ai/generate-summary", json={"patient_id": pid, "workflow_id": wf[pid]}, headers=physician)
        claim = {"patient_id": pid, "insurer_name": "Acme", "policy_number": "P", "billed_amount": 10.0}
        assert (await client.post("/api/insurance/claims", json=claim, headers=admin)).status_code == 200

    async def visible():
        return {
            "patients": {p["patient_id"] for p in (await client.get("/api/patients", headers=nurse)).json()},
            "workflows": {x["patient_id"] for x in (await client.get("/api/workflows", headers=nurse)).json()},
            "tasks": {t["patient_id"] for t in (await client.get("/api/tasks", headers=nurse)).json()},
            "claims": {c["patient_id"] for c in (await client.get("/api/insurance/claims", headers=nurse)).json()},
        }

    assert await visible() == {"patients": {west, pool}, "workflows": {west}, "tasks": {west}, "claims": {west}}
    resp = await client.get(f"/api/patients/{icu}", headers=nurse)
    assert resp.status_code == 403 and resp.json()["break_glass"] is True
    assert (await client.get("/api/patients/pat_missing", headers=nurse)).status_code == 404
    assert (await client.get(f"/api/workflows/{wf[icu]}", headers=nurse)).status_code == 404
    assert (await client.get(f"/api/workflows/patient/{icu}", headers=nurse)).json() is None
    assert (await client.get(f"/api/ai/summary/{wf[icu]}", headers=nurse)).json() is None
    assert (await client.put(f"/api/patients/{icu}", json={"room_number": "9"}, headers=nurse)).status_code == 404
    fhir = (await client.get("/api/fhir/R4/Patient", headers=nurse)).json()
    assert icu not in {e["resource"]["id"] for e in fhir.get("entry", [])}
    # The physician on both units and the admin see everything.
    assert len((await client.get("/api/patients", headers=physician)).json()) == 3
    assert len((await client.get("/api/patients", headers=admin)).json()) == 3

    # Break-glass opens that one patient and everything about them, until the grant expires.
    resp = await client.post(f"/api/patients/{icu}/break-glass", json={"reason": "Covering ICU for a colleague"},
                             headers=nurse)
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/patients/{icu}", headers=nurse)).json()["name"] == "Icu Patient"
    assert (await client.get(f"/api/workflows/{wf[icu]}", headers=nurse)).json()["discharge_summary"] is not None
    assert (await visible())["claims"] == {west, icu}

    await db.execute(update(AccessGrant).values(expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    assert (await client.get(f"/api/patients/{icu}", headers=nurse)).status_code == 403


@requires_postgres
async def test_rls_scope_is_off_without_the_flag(client, create_patient, ward):
    w = await ward()
    _, nurse = await w["member"]("nurse", "4W")
    await create_patient(w["admin"], unit_id=w["units"]["ICU"])
    assert len((await client.get("/api/patients", headers=nurse)).json()) == 1


async def make_physician(w):
    return await w["member"]("physician", "4W", "ICU")
