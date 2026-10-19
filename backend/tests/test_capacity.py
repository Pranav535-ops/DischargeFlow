# ruff: noqa: F811  (the make_client and send fixtures are imported from test_hl7_adt)
"""ADR-002 Phase 9, item 17: capacity board (expected discharges by unit and hour, discharge before noon) and the
bed-to-cleaning handoff."""
import dataclasses
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select, update

from app.audit.models import AuditLog
from app.capacity.models import BedTurnover
from app.core import security
from app.core.db import utcnow
from app.discharge.models import Workflow
from app.discharge.templates import hospital_today, hospital_zone
from app.patients.models import Encounter
from tests.conftest import requires_postgres
from tests.test_hl7_adt import adt, make_client, send, with_pv1  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture
async def units(client, make_user):
    _, admin = await make_user("admin")
    ids = {}
    for code in ("4W", "ICU"):
        resp = await client.post("/api/units", json={"code": code, "name": f"Unit {code}"}, headers=admin)
        assert resp.status_code == 200, resp.text
        ids[code] = resp.json()["unit_id"]
    return ids, admin


async def _encounter(db, patient_id) -> Encounter:
    db.expire_all()
    return await db.scalar(select(Encounter).where(Encounter.patient_id == patient_id)
                           .order_by(Encounter.created_at.desc()).limit(1))


async def _audit(db, action) -> list[AuditLog]:
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


def _at(hour: int, day=None) -> datetime:
    return datetime.combine(day or hospital_today(), time(hour), hospital_zone())


async def test_manual_discharge_dirties_the_bed_and_evs_turns_it_over(client, make_user, create_patient, db, units):
    unit_ids, admin = units
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse, unit_id=unit_ids["4W"], room_number="401"))["patient_id"]
    resp = await client.put(f"/api/patients/{pid}", json={"status": "discharged"}, headers=nurse)
    assert resp.status_code == 200, resp.text

    [bed] = (await client.get("/api/beds", headers=nurse)).json()
    assert (bed["unit_id"], bed["room_number"], bed["status"], bed["source"]) == (unit_ids["4W"], "401", "dirty",
                                                                               "discharge")
    assert "encounter_id" not in bed  # the board never links a bed to the patient who left it

    path = f"/api/beds/{bed['turnover_id']}"
    _, physician = await make_user("physician")
    assert (await client.put(path, json={"status": "cleaning"}, headers=physician)).status_code == 403
    assert (await client.put(path, json={"status": "dirty"}, headers=nurse)).status_code == 422
    cleaning = (await client.put(path, json={"status": "cleaning"}, headers=nurse)).json()
    assert cleaning["status"] == "cleaning" and cleaning["cleaning_started_at"]
    assert (await client.put(path, json={"status": "cleaning"}, headers=nurse)).status_code == 409
    done = (await client.put(path, json={"status": "available"}, headers=nurse)).json()
    assert done["status"] == "available" and done["available_at"]
    assert (await client.put(path, json={"status": "cleaning"}, headers=nurse)).status_code == 409
    assert (await client.get("/api/beds", headers=nurse)).json() == []

    entries = await _audit(db, "BED_STATUS")
    assert [e.details for e in entries] == [f"unit={unit_ids['4W']} dirty->cleaning",
                                            f"unit={unit_ids['4W']} cleaning->available"]
    assert all(e.resource_type == "bed" and "401" not in e.details for e in entries)


async def test_discharge_completion_and_adt_a03_dirty_the_bed(client, make_user, make_client, send, db, units):
    from tests.test_adt_events import _finish_documentation, _patient, _start_discharge

    _, key, _ = await make_client()
    # A03 with no workflow.
    assert (await send(key, adt("A01", "B1")))["code"] == "AA"
    assert (await send(key, adt("A03", "B2")))["code"] == "AA"
    [first] = list(await db.scalars(select(BedTurnover)))
    assert (first.room_number, first.unit_id, first.status) == ("412-B", units[0]["4W"], "dirty")

    # The same bed is not opened twice while it is still dirty: readmitted to it and discharged in the UI.
    assert (await send(key, adt("A01", "B3")))["code"] == "AA"
    patient = await _patient(db)
    wf_id, physician = await _start_discharge(client, make_user, patient.patient_id)
    await _finish_documentation(client, physician, patient.patient_id, wf_id)
    assert (await client.put(f"/api/workflows/{wf_id}/complete", headers=physician)).status_code == 200
    db.expire_all()
    assert [t.turnover_id for t in await db.scalars(select(BedTurnover))] == [first.turnover_id]

    # A transfer out of a room dirties it; A13 (cancel discharge) cancels a turnover nobody started.
    await db.execute(update(BedTurnover).values(status="available", available_at=utcnow()))
    await db.commit()
    assert (await send(key, adt("A01", "B4")))["code"] == "AA"
    assert (await send(key, with_pv1(adt("A02", "B5"), f3="ICU^7^A")))["code"] == "AA"
    assert (await send(key, adt("A03", "B6")))["code"] == "AA"
    db.expire_all()
    rows = {(t.room_number, t.source): t.status for t in await db.scalars(select(BedTurnover))}
    assert rows == {("412-B", "discharge"): "available", ("412-B", "transfer"): "dirty",
                    ("7-A", "discharge"): "dirty"}
    assert (await send(key, adt("A13", "B7")))["code"] == "AA"
    db.expire_all()
    rows = {(t.room_number, t.source): t.status for t in await db.scalars(select(BedTurnover))}
    assert rows[("7-A", "discharge")] == "cancelled" and rows[("412-B", "transfer")] == "dirty"


async def test_evs_system_advances_beds_with_an_api_key(client, make_client, make_user, db, units):
    unit_ids, admin = units
    _, key, _ = await make_client("EVS")
    resp = await client.post("/api/beds", json={"unit_id": unit_ids["ICU"], "room_number": "9"}, headers=key)
    assert resp.status_code == 200, resp.text
    bed = resp.json()
    assert bed["source"] == "manual"
    # Reporting it again returns the open turnover instead of a second one.
    again = (await client.post("/api/beds", json={"unit_id": unit_ids["ICU"], "room_number": "9"}, headers=key))
    assert again.json()["turnover_id"] == bed["turnover_id"]
    assert (await client.post("/api/beds", json={"unit_id": "unit_nope", "room_number": "9"},
                              headers=key)).status_code == 400
    resp = await client.put(f"/api/beds/{bed['turnover_id']}", json={"status": "available"}, headers=key)
    assert resp.status_code == 200 and resp.json()["cleaning_started_at"] is None
    # An integration client has no capacity:read, and a bad key is refused.
    assert (await client.get("/api/capacity/board", headers=key)).status_code == 401
    assert (await client.get("/api/beds", headers={"X-API-Key": "default.nope.nope"})).status_code == 401
    [entry] = await _audit(db, "BED_DIRTY")
    assert entry.details == f"unit={unit_ids['ICU']} source=manual"


async def test_board_counts_expected_discharges_by_unit_and_hour(client, make_user, create_patient, db, units):
    unit_ids, admin = units
    _, physician = await make_user("physician")
    today = hospital_today()
    plan = {"expected_discharge_date": today.isoformat()}
    ids = {}
    for name, unit, room in (("a", "4W", "1"), ("b", "4W", "2"), ("c", "4W", "3"), ("d", "ICU", "4"),
                             ("e", None, "5"), ("f", "4W", "6")):
        ids[name] = (await create_patient(physician, unit_id=unit_ids.get(unit or ""), room_number=room))["patient_id"]
    for name in ("a", "b", "d"):
        enc = await _encounter(db, ids[name])
        assert (await client.put(f"/api/encounters/{enc.encounter_id}/plan", json=plan,
                                 headers=physician)).status_code == 200
    enc_c = await _encounter(db, ids["c"])
    await client.put(f"/api/encounters/{enc_c.encounter_id}/plan", headers=physician,
                     json={"expected_discharge_date": (today + timedelta(days=1)).isoformat()})
    # a has a workflow estimated at 10:00 today; b's plan has no time (unscheduled).
    resp = await client.post(f"/api/workflows/initiate/{ids['a']}", headers=physician)
    assert resp.status_code == 200, resp.text
    await db.execute(update(Workflow).where(Workflow.patient_id == ids["a"])
                     .values(estimated_discharge_time=_at(10)))
    # f left at 09:00 today (before noon); its bed is dirty.
    enc_f = await _encounter(db, ids["f"])
    await db.execute(update(Encounter).where(Encounter.encounter_id == enc_f.encounter_id)
                     .values(status="discharged", discharged_at=_at(9)))
    db.add(BedTurnover(tenant_id="default", unit_id=unit_ids["4W"], room_number="6", dirty_at=_at(9)))
    await db.commit()

    _, pharmacist = await make_user("pharmacist")
    assert (await client.get("/api/capacity/board", headers=pharmacist)).status_code == 403
    _, nurse = await make_user("nurse")
    body = (await client.get("/api/capacity/board", headers=nurse)).json()
    assert body["date"] == today.isoformat() and body["noon_hour"] == 12
    cols = {c["unit_code"]: c for c in body["units"]}
    assert list(cols) == ["4W", "ICU", None]  # unassigned last
    west = cols["4W"]
    assert (west["in_house"], west["expected"], west["unscheduled"], west["expected_tomorrow"]) == (3, 2, 1, 1)
    assert west["expected_by_hour"] == {"10": 1}
    assert (west["discharged"], west["discharged_before_noon"], west["discharged_by_hour"]) == (1, 1, {"9": 1})
    assert west["before_noon_7d"] == {"n": 1, "before_noon": 1, "rate": 1.0}
    assert west["beds_dirty"] == 1
    assert cols["ICU"]["expected"] == 1 and cols["ICU"]["unscheduled"] == 1
    assert cols[None]["no_expected_date"] == 1
    totals = body["totals"]
    assert (totals["in_house"], totals["expected"], totals["beds_dirty"]) == (5, 3, 1)
    assert totals["expected_by_hour"] == {"10": 1}
    [bed] = body["beds"]
    assert bed["room_number"] == "6" and "encounter_id" not in bed
    # Another day: what was expected then, nothing discharged.
    other = (await client.get(f"/api/capacity/board?date={(today + timedelta(days=1)).isoformat()}",
                              headers=nurse)).json()
    assert {c["unit_code"]: c["expected"] for c in other["units"]}["4W"] == 1


# ---------- unit scope (Postgres RLS) ----------

@requires_postgres
async def test_board_and_beds_follow_unit_scope(client, make_user, create_patient, db, units, monkeypatch):
    unit_ids, admin = units
    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, unit_scoped_access=True))
    _, physician = await make_user("physician")
    for unit, room in (("4W", "1"), ("ICU", "2")):
        pid = (await create_patient(admin, unit_id=unit_ids[unit], room_number=room))["patient_id"]
        assert (await client.put(f"/api/patients/{pid}", json={"status": "discharged"},
                                 headers=admin)).status_code == 200
    await create_patient(admin, unit_id=unit_ids["ICU"], room_number="3")

    nurse_doc, nurse = await make_user("nurse")
    resp = await client.put(f"/api/users/{nurse_doc['user_id']}/units", json={"unit_ids": [unit_ids["4W"]]},
                            headers=admin)
    assert resp.status_code == 200, resp.text
    body = (await client.get("/api/capacity/board", headers=nurse)).json()
    assert body["unit_scoped"] is True
    assert [c["unit_code"] for c in body["units"]] == ["4W"]
    assert [b["room_number"] for b in body["beds"]] == ["1"]
    beds = (await client.get("/api/beds", headers=nurse)).json()
    assert [b["room_number"] for b in beds] == ["1"]  # RLS hides the ICU bed
    icu_bed = await db.scalar(select(BedTurnover).where(BedTurnover.room_number == "2"))
    resp = await client.put(f"/api/beds/{icu_bed.turnover_id}", json={"status": "cleaning"}, headers=nurse)
    assert resp.status_code == 404
    assert (await client.post("/api/beds", json={"unit_id": unit_ids["ICU"], "room_number": "8"},
                              headers=nurse)).status_code == 403
    # The admin sees the whole tenant.
    admin_body = (await client.get("/api/capacity/board", headers=admin)).json()
    assert {c["unit_code"] for c in admin_body["units"]} == {"4W", "ICU"}
    assert sorted(b["room_number"] for b in admin_body["beds"]) == ["1", "2"]
