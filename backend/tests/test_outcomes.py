"""ADR-002 Phase 9, item 19: outcome analytics (aggregate only, groups under MIN_GROUP_SIZE without rates)."""
import json
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select, update

from app.analytics.outcomes import pareto
from app.core.db import utcnow
from app.discharge.models import DelayReason
from app.discharge.templates import hospital_zone
from app.patients.models import Encounter
from tests.conftest import DEFAULT_TENANT

pytestmark = pytest.mark.anyio


def _local(days_ago: int, hour: int) -> datetime:
    day = (utcnow() - timedelta(days=days_ago)).astimezone(hospital_zone()).date()
    return datetime.combine(day, time(hour), hospital_zone())


async def _encounter(db, patient_id) -> Encounter:
    db.expire_all()
    return await db.scalar(select(Encounter).where(Encounter.patient_id == patient_id))


async def test_outcomes_by_unit_with_small_groups_suppressed(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    units = {}
    for code in ("4W", "ICU"):
        units[code] = (await client.post("/api/units", json={"code": code, "name": code}, headers=admin)).json()["unit_id"]
    ids = {}
    for name, unit in (("a", "4W"), ("b", "4W"), ("c", "4W"), ("d", "ICU")):
        ids[name] = (await create_patient(admin, name=f"Patient {name.upper()}", unit_id=units[unit]))["patient_id"]

    # (discharged days ago, hour, admitted days before discharge, final EDD offset from the discharge day, first)
    stays = {"a": (40, 9, 10, 0, -2), "b": (38, 14, 8, -1, -1), "c": (2, 10, 5, None, None), "d": (45, 15, 3, 1, None)}
    for name, (ago, hour, los, final, first) in stays.items():
        left = _local(ago, hour)
        day = left.date()
        enc = await _encounter(db, ids[name])
        await db.execute(update(Encounter).where(Encounter.encounter_id == enc.encounter_id).values(
            status="discharged", discharged_at=left, admitted_at=left - timedelta(days=los),
            expected_discharge_date=None if final is None else day + timedelta(days=final),
            first_expected_discharge_date=None if first is None else day + timedelta(days=first)))
        if name == "a":  # readmitted five days later
            a_stay, patient = enc.encounter_id, enc.patient
            readmission = Encounter(patient=patient, patient_id=patient.patient_id, unit_id=units["4W"],
                                    status="active", admitted_at=left + timedelta(days=5))
            db.add(readmission)
    for code, n in (("social", 3), ("placement", 1)):
        for _ in range(n):
            db.add(DelayReason(tenant_id=DEFAULT_TENANT, patient_id=ids["a"], encounter_id=a_stay,
                               kind="expected_date_slip", code=code, slip_days=1, recorded_by="u"))
    db.add(DelayReason(tenant_id=DEFAULT_TENANT, patient_id=ids["b"], kind="task_sla_breach", code="pharmacy",
                       recorded_by="u"))
    await db.commit()

    _, nurse = await make_user("nurse")
    assert (await client.get("/api/analytics/outcomes", headers=nurse)).status_code == 403
    resp = await client.get("/api/analytics/outcomes?days=90", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    raw = json.dumps(body)
    assert not any(pid in raw for pid in ids.values()) and "Patient" not in raw  # no patient-level data

    overall = body["overall"]
    assert overall["discharges"] == 4
    assert overall["before_noon"] == {"n": 4, "count": 2, "rate": 0.5}
    assert overall["length_of_stay_days"]["n"] == 4 and overall["length_of_stay_days"]["median_days"] == 6.5
    # d (left from ICU, a day early) is in the overall numbers and the ICU's, which is too small for rates.
    assert overall["vs_expected_date"] == {"n": 3, "avg_days": 0.0, "median_days": 0.0,
                                           "on_or_before": {"n": 3, "count": 2, "rate": 0.667}}
    accuracy = overall["expected_date_accuracy"]
    assert (accuracy["with_expected_date"], accuracy["with_first_expected_date"],
            accuracy["first_estimate_changed"]) == (3, 2, 1)
    assert accuracy["final_exact"] == {"n": 3, "count": 1, "rate": 0.333}
    assert accuracy["first_exact"] == {"n": 2, "count": 0}  # under 3: no rate
    assert accuracy["final_abs_error"] == {"n": 3, "avg_days": 0.67, "median_days": 1.0}
    assert overall["readmitted_within_30_days"] == {"n": 3, "count": 1, "rate": 0.333, "follow_up_pending": 1}

    icu = body["by_unit"]["ICU"]
    assert icu["discharges"] == 1 and icu["before_noon"] == {"n": 1, "count": 0}
    assert icu["length_of_stay_days"] == {"n": 1}
    assert body["by_unit"]["4W"]["discharges"] == 3

    delays = body["delay_reasons"]
    assert delays["recorded"] == 5
    assert delays["pareto"] == [
        {"code": "social", "n": 3, "share": 0.6, "cumulative_share": 0.6},
        {"code": "pharmacy", "n": 1, "share": 0.2, "cumulative_share": 0.8},  # ties by code
        {"code": "placement", "n": 1, "share": 0.2, "cumulative_share": 1.0},
    ]
    assert [r["code"] for r in delays["by_kind"]["task_sla_breach"]] == ["pharmacy"]
    assert delays["by_unit"] == {"4W": {"n": 4, "by_code": {"placement": 1, "social": 3}},
                                 "unassigned": {"n": 1, "by_code": {"pharmacy": 1}}}

    # A shorter window leaves the older stays out.
    recent = (await client.get("/api/analytics/outcomes?days=7", headers=admin)).json()
    assert recent["overall"]["discharges"] == 1 and recent["delay_reasons"]["recorded"] == 5


async def test_first_expected_date_is_kept_when_the_plan_moves(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    enc = await _encounter(db, pid)
    first = (utcnow() + timedelta(days=2)).date()
    path = f"/api/encounters/{enc.encounter_id}/plan"
    assert (await client.put(path, json={"planned_disposition": "home"}, headers=nurse)).status_code == 200
    assert (await _encounter(db, pid)).first_expected_discharge_date is None
    assert (await client.put(path, json={"expected_discharge_date": first.isoformat()},
                             headers=nurse)).status_code == 200
    resp = await client.put(path, headers=nurse, json={
        "expected_discharge_date": (first + timedelta(days=3)).isoformat(), "delay_code": "placement"})
    assert resp.status_code == 200, resp.text
    enc = await _encounter(db, pid)
    assert enc.first_expected_discharge_date == first
    assert enc.expected_discharge_date == first + timedelta(days=3)


def test_pareto_orders_by_count_then_code():
    from collections import Counter
    assert pareto(Counter()) == []
    assert [r["code"] for r in pareto(Counter({"b": 1, "a": 1, "c": 2}))] == ["c", "a", "b"]
