# ruff: noqa: F811  (the make_client and send fixtures are imported from test_hl7_adt)
"""ADR-002 item 20: readmission risk flag at planning time from the LACE index (van Walraven et al., CMAJ 2010)."""
import itertools
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.audit.models import AuditLog
from app.core import crypto
from app.discharge.models import Task
from app.patients.models import Encounter, Patient
from app.risk import lace
from app.risk import service as risk
from tests.test_hl7_adt import MRN, adt, make_client, send, with_pv1  # noqa: F401

TODAY = date.today()
ALL_COMORBIDITIES = list(lace.COMORBIDITIES)


# ---------- the score itself (pure, every band boundary) ----------

@pytest.mark.parametrize("days, points", [
    (0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 4), (6, 4), (7, 5), (10, 5), (13, 5), (14, 7), (15, 7), (365, 7)])
def test_length_of_stay_bands(days, points):
    assert lace.length_of_stay_points(days) == points


@pytest.mark.parametrize("code, points", [
    ("E", 3), ("U", 3), ("A", 3), ("e", 3), (" E ", 3),
    ("C", 0), ("R", 0), ("L", 0), ("N", 0), ("", 0), (None, 0), ("X", 0)])
def test_acuity(code, points):
    assert lace.acuity_points(code) == points


@pytest.mark.parametrize("charlson, points", [(0, 0), (1, 1), (2, 2), (3, 3), (4, 5), (5, 5), (12, 5), (40, 5)])
def test_comorbidity_bands(charlson, points):
    assert lace.comorbidity_points(charlson) == points


@pytest.mark.parametrize("visits, points", [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 4), (30, 4)])
def test_ed_visits_are_capped_at_four(visits, points):
    assert lace.ed_visit_points(visits) == points


@pytest.mark.parametrize("fn", [lace.length_of_stay_points, lace.comorbidity_points, lace.ed_visit_points])
def test_negative_inputs_are_refused(fn):
    with pytest.raises(ValueError):
        fn(-1)


def test_charlson_weights_and_grades():
    assert lace.charlson_index([]) == 0
    assert lace.charlson_index(["myocardial_infarction"]) == 1
    assert lace.charlson_index(["congestive_heart_failure", "dementia"]) == 5
    assert lace.charlson_index(["metastatic_solid_tumor"]) == 6
    # Each condition once; unknown keys don't score.
    assert lace.charlson_index(["dementia", "dementia", "not_a_condition"]) == 3
    # Two grades of one condition: only the worse counts.
    assert lace.charlson_index(["diabetes_uncomplicated", "diabetes_end_organ_damage"]) == 2
    assert lace.charlson_index(["mild_liver_disease", "moderate_severe_liver_disease"]) == 4
    assert lace.charlson_index(["any_tumor", "metastatic_solid_tumor"]) == 6
    assert lace.counted_comorbidities(["any_tumor", "metastatic_solid_tumor", "aids"]) == [
        "aids", "metastatic_solid_tumor"]
    assert lace.unknown_comorbidities(["aids", "gout"]) == ["gout"]
    assert all(weight in (1, 2, 3, 4, 6) for _, weight in lace.COMORBIDITIES.values())


def test_score_range_threshold_and_missing_inputs():
    top = lace.score(lace.LaceInputs(30, "E", ALL_COMORBIDITIES, 9))
    assert (top.score, top.length_of_stay, top.acuity, top.comorbidity, top.ed_visits) == (19, 7, 3, 5, 4)
    assert top.high_risk and top.complete and top.version == lace.SCORE_VERSION
    bottom = lace.score(lace.LaceInputs(0, "C", [], 0))
    assert bottom.score == 0 and not bottom.high_risk and bottom.complete
    # 9 is not high risk, 10 is (L 4 + A 3 + C 2 = 9; one ED visit makes 10).
    assert lace.score(lace.LaceInputs(5, "E", ["congestive_heart_failure"], 0)).score == 9
    assert not lace.is_high_risk(9) and lace.is_high_risk(10)
    assert lace.score(lace.LaceInputs(5, "E", ["congestive_heart_failure"], 1)).high_risk
    # Unrecorded inputs score 0 and are named, so a partial total reads as a lower bound.
    partial = lace.score(lace.LaceInputs(None, None, None, None))
    assert partial.score == 0 and partial.missing == ["length_of_stay", "acuity", "comorbidity", "ed_visits"]
    assert not partial.complete


def test_every_combination_stays_within_0_to_19():
    scores = {lace.score(lace.LaceInputs(days, code, conds, visits)).score
              for days, code, conds, visits in itertools.product(
                  range(0, 16), ("E", "C"), ([], ["dementia"], ["aids", "metastatic_solid_tumor"]), range(0, 6))}
    assert min(scores) == 0 and max(scores) == lace.MAX_SCORE == 19


# ---------- on the discharge plan ----------

async def _stay(client, make_user, create_patient, *, admitted_days_ago=2, role="nurse"):
    _, headers = await make_user(role)
    patient = await create_patient(headers, admission_date=(TODAY - timedelta(days=admitted_days_ago)).isoformat())
    [encounter] = (await client.get(f"/api/patients/{patient['patient_id']}/encounters", headers=headers)).json()
    return patient["patient_id"], encounter["encounter_id"], headers


async def _high_risk(client, enc, headers):
    """L 4 (5-day expected stay) + A 3 (emergency) + C 5 (CHF 2 + dementia 3) + E 2 (recorded) = 14."""
    resp = await client.put(f"/api/encounters/{enc}/plan", headers=headers,
                            json={"expected_discharge_date": (TODAY + timedelta(days=3)).isoformat()})
    assert resp.status_code == 200, resp.text
    resp = await client.put(f"/api/encounters/{enc}/risk-inputs", headers=headers, json={
        "admission_type": "E", "comorbidities": ["dementia", "congestive_heart_failure"], "ed_visits_recorded": 2})
    assert resp.status_code == 200, resp.text
    return resp.json()["readmission_risk"]


@pytest.mark.anyio
async def test_new_stay_is_scored_with_what_is_known(client, make_user, create_patient, db):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    plan = (await client.get(f"/api/encounters/{enc}/plan", headers=nurse)).json()
    r = plan["readmission_risk"]
    # Two days so far (no expected date); no admission type or comorbidities recorded; no prior ED visits.
    assert (r["score"], r["high_risk"], r["complete"]) == (2, False, False)
    assert r["missing"] == ["acuity", "comorbidity"]
    assert r["method"] == "LACE index" and "doi:10.1503/cmaj.091117" in r["citation"]
    assert [c["letter"] for c in r["components"]] == ["L", "A", "C", "E"]
    los, acuity, comorbidity, ed = r["components"]
    assert (los["points"], los["max_points"]) == (2, 7) and los["explanation"].startswith("2 days so far")
    assert acuity["explanation"].startswith("Not recorded")
    assert comorbidity["explanation"].startswith("Not recorded")
    assert ed["explanation"] == "0 visits, counted from this system's encounters"
    assert r["suggested_tasks"] == []
    # The read is one PHI_READ entry, as every plan read.
    reads = list(await db.scalars(select(AuditLog).where(AuditLog.action == "PHI_READ",
                                                         AuditLog.resource_id == pid)))
    assert reads


@pytest.mark.anyio
async def test_inputs_recompute_explain_and_stay_out_of_the_audit(client, make_user, create_patient, db):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    r = await _high_risk(client, enc, nurse)
    assert (r["score"], r["high_risk"], r["complete"], r["threshold"]) == (14, True, True, 10)
    explained = {c["key"]: (c["points"], c["explanation"]) for c in r["components"]}
    assert explained["length_of_stay"] == (4, "5 days to the expected discharge date")
    assert explained["acuity"] == (3, "Emergency admission: unplanned (acute)")
    assert explained["comorbidity"] == (5, "Charlson index 5: Congestive heart failure 2, Dementia 3")
    assert explained["ed_visits"] == (2, "2 visits, recorded by staff from the hospital's records")
    assert r["inputs"]["comorbidities"] == ["congestive_heart_failure", "dementia"]

    # Encrypted at rest under the patient's key; the stored detail names no condition.
    db.expire_all()
    encounter = await db.get(Encounter, enc)
    assert crypto.is_encrypted(encounter._comorbidities) and "dementia" not in encounter._comorbidities
    assert "dementia" not in str(encounter.readmission_score_detail)
    assert encounter.readmission_score == 14 and encounter.readmission_score_version == lace.SCORE_VERSION

    [entry] = list(await db.scalars(select(AuditLog).where(AuditLog.action == "UPDATE_RISK_INPUTS")))
    assert entry.resource_id == pid
    assert entry.details == (f"encounter={enc} fields: admission_type, comorbidities, ed_visits_recorded "
                             f"version={lace.SCORE_VERSION}")

    # The expected date is the L input: discharging today makes it a 2-day stay (L 2), total 12.
    resp = await client.put(f"/api/encounters/{enc}/plan", headers=nurse,
                            json={"expected_discharge_date": TODAY.isoformat()})
    assert resp.json()["readmission_risk"]["score"] == 12
    # Clearing the recorded ED count goes back to counting encounters (none), and a planned admission scores 0.
    resp = await client.put(f"/api/encounters/{enc}/risk-inputs", headers=nurse,
                            json={"ed_visits_recorded": None, "admission_type": "C"})
    r = resp.json()["readmission_risk"]
    assert r["score"] == 7 and not r["high_risk"] and r["suggested_tasks"] == []


@pytest.mark.anyio
async def test_risk_inputs_are_validated_and_need_plan_manage(client, make_user, create_patient):
    _, enc, nurse = await _stay(client, make_user, create_patient)
    bad = [{"admission_type": "Z"}, {"comorbidities": ["gout"]}, {"ed_visits_recorded": -1},
           {"patient_class": "emergency"}]
    for body in bad:
        assert (await client.put(f"/api/encounters/{enc}/risk-inputs", headers=nurse, json=body)).status_code == 422
    _, pharmacist = await make_user("pharmacist")
    resp = await client.put(f"/api/encounters/{enc}/risk-inputs", headers=pharmacist, json={"admission_type": "E"})
    assert resp.status_code == 403
    codes = (await client.get("/api/discharge-plans/codes", headers=pharmacist)).json()
    assert {c["code"] for c in codes["admission_types"] if c["acute"]} == {"A", "E", "U"}
    assert {c["key"]: c["weight"] for c in codes["comorbidities"]}["metastatic_solid_tumor"] == 6
    assert codes["readmission_risk"]["threshold"] == 10


@pytest.mark.anyio
async def test_high_risk_suggests_tasks_the_clinician_chooses(client, make_user, create_patient, db):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    r = await _high_risk(client, enc, nurse)
    assert [(s["key"], s["added"]) for s in r["suggested_tasks"]] == [
        ("follow_up_7_days", False), ("medication_teach_back", False), ("post_discharge_call", False)]
    assert not list(await db.scalars(select(Task).where(Task.patient_id == pid)))  # nothing added on its own

    resp = await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse, json={"keys": ["follow_up_7_days"]})
    assert resp.status_code == 200, resp.text
    [task] = resp.json()["added"]
    assert task["title"] == "Follow-up Appointment Within 7 Days" and task["assigned_role"] == "nurse"
    assert task["due_at"] and task["due_offset_hours"] == 10
    # Each once.
    again = await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse, json={"keys": ["follow_up_7_days"]})
    assert again.json()["added"] == []
    plan = (await client.get(f"/api/encounters/{enc}/plan", headers=nurse)).json()
    assert [s["added"] for s in plan["readmission_risk"]["suggested_tasks"]] == [True, False, False]
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "ADD_RISK_TASKS"))
    assert entry.details == f"encounter={enc} tasks: {task['task_id']}"

    bad = await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse, json={"keys": ["daily_weights"]})
    assert bad.status_code == 422


@pytest.mark.anyio
async def test_suggested_tasks_join_the_running_discharge_workflow(client, make_user, create_patient):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    await _high_risk(client, enc, nurse)
    _, physician = await make_user("physician")
    wf = (await client.post(f"/api/workflows/initiate/{pid}", headers=physician)).json()["workflow"]
    resp = await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse,
                             json={"keys": ["medication_teach_back", "post_discharge_call"]})
    assert {t["workflow_id"] for t in resp.json()["added"]} == {wf["workflow_id"]}
    detail = (await client.get(f"/api/workflows/{wf['workflow_id']}", headers=nurse)).json()
    assert "Medication Teach-back" in {t["title"] for t in detail["tasks"]}


@pytest.mark.anyio
async def test_low_risk_gets_no_suggestions(client, make_user, create_patient):
    _, enc, nurse = await _stay(client, make_user, create_patient)
    resp = await client.post(f"/api/encounters/{enc}/risk-tasks", headers=nurse, json={"keys": ["follow_up_7_days"]})
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_review_list_flags_high_risk(client, make_user, create_patient):
    _, enc, nurse = await _stay(client, make_user, create_patient)
    await _high_risk(client, enc, nurse)
    units = (await client.get("/api/discharge-plans/review", headers=nurse)).json()["units"]
    [row] = [p for u in units for p in u["plans"] if p["encounter_id"] == enc]
    assert row["readmission_risk"] == {"score": 14, "high_risk": True, "complete": True}


@pytest.mark.anyio
async def test_discharge_scores_the_actual_stay(client, make_user, create_patient, db):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    await _high_risk(client, enc, nurse)  # expected stay 5 days
    _, admin = await make_user("admin")
    resp = await client.put(f"/api/patients/{pid}", headers=admin, json={"status": "discharged"})
    assert resp.status_code == 200, resp.text
    db.expire_all()
    encounter = await db.get(Encounter, enc)
    assert encounter.readmission_score_detail["length_of_stay"] == {"points": 2, "days": 2, "basis": "actual"}
    assert encounter.readmission_score == 12


@pytest.mark.anyio
async def test_a_score_from_an_older_version_is_recomputed_on_read(client, make_user, create_patient, db):
    _, enc, nurse = await _stay(client, make_user, create_patient)
    await db.execute(update(Encounter).where(Encounter.encounter_id == enc).values(
        readmission_score=None, readmission_score_version=None, readmission_score_detail=None))
    await db.commit()
    plan = (await client.get(f"/api/encounters/{enc}/plan", headers=nurse)).json()
    assert plan["readmission_risk"]["version"] == lace.SCORE_VERSION and plan["readmission_risk"]["score"] == 2


@pytest.mark.anyio
async def test_erasure_overwrites_the_comorbidities(client, make_user, create_patient, db):
    pid, enc, nurse = await _stay(client, make_user, create_patient)
    await _high_risk(client, enc, nurse)
    _, admin = await make_user("admin")
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200
    db.expire_all()
    encounter = await db.get(Encounter, enc)
    assert encounter._comorbidities == "[REDACTED]" and encounter.comorbidities is None


def test_the_summary_prompt_facts_carry_no_risk():
    """Never sent to a model: the summary's facts are an explicit list, and app.ai can't import app.risk."""
    import inspect

    from app.ai import service as ai_service
    assert "readmission" not in inspect.getsource(ai_service.summary_facts)
    contracts = (Path(__file__).resolve().parents[1] / ".importlinter").read_text()
    assert "[importlinter:contract:risk-not-for-models]" in contracts


# ---------- from the ADT feed ----------

@pytest.mark.anyio
async def test_adt_admission_type_and_prior_emergency_visits(make_client, send, db):
    _, key, _ = await make_client()
    # An emergency admission on 17 Oct, discharged; readmitted on 19 Oct as urgent, through the ED (class E).
    await send(key, with_pv1(adt("A01", "R1"), f4="E", f19="V1"))
    await send(key, adt("A03", "R2"))
    await send(key, with_pv1(adt("A01", "R3"), f2="E", f4="U", f19="V2", f44="20261019090000"))
    db.expire_all()
    patient = next(p for p in await db.scalars(select(Patient)) if p.mrn == MRN)
    current = await db.scalar(select(Encounter).where(Encounter.patient_id == patient.patient_id,
                                                      Encounter.status == "active"))
    current_id = current.encounter_id
    assert (current.admission_type, current.patient_class) == ("U", "E")
    detail = current.readmission_score_detail
    assert detail["acuity"] == {"points": 3, "admission_type": "U"}
    assert detail["ed_visits"] == {"points": 1, "visits": 1, "source": "encounters"}
    assert detail["ed_visits_counted"] == 1

    # A08 carries a corrected admission type: the stay is rescored, the ACK names the field only.
    ack = await send(key, with_pv1(adt("A08", "R4"), f2="E", f4="C", f19="V2", f44="20261019090000"))
    assert (ack["code"], ack["text"]) == ("AA", "Patient updated: admission_type")
    db.expire_all()
    current = await db.get(Encounter, current_id)
    assert current.readmission_score_detail["acuity"]["points"] == 0
    # An admission type outside HL7 table 0007 is dropped, not guessed.
    from app.interop.adt import acuity_from
    from app.interop.hl7 import parse
    assert acuity_from(parse(with_pv1(adt("A01", "R5"), f4="9"))) == {"patient_class": "I"}


@pytest.mark.anyio
async def test_a40_rekeys_comorbidities_and_counts_the_merged_history(make_client, send, db):
    _, key, _ = await make_client()
    await send(key, with_pv1(adt("A01", "M1", mrn="MRN0099"), f4="E", f19="V-OLD"))
    await send(key, adt("A03", "M2", mrn="MRN0099"))
    db.expire_all()
    prior_stay = await db.scalar(select(Encounter))
    prior_stay.comorbidities = ["dementia"]
    await db.commit()
    await send(key, with_pv1(adt("A01", "M3"), f19="V-NEW", f44="20261019090000"))
    db.expire_all()
    survivor = next(p for p in await db.scalars(select(Patient)) if p.mrn == MRN)
    active = await db.scalar(select(Encounter).where(Encounter.status == "active"))
    assert active.readmission_score_detail["ed_visits_counted"] == 0

    ack = await send(key, adt("A40", "M4") + "MRG|MRN0099^^^GENHOSP^MR\r")
    assert ack["text"] == "Records merged"
    db.expire_all()
    stays = {e.status: e for e in await db.scalars(select(Encounter))}
    assert stays["discharged"].patient_id == survivor.patient_id
    assert stays["discharged"].comorbidities == ["dementia"]  # re-encrypted under the survivor's key
    assert stays["active"].readmission_score_detail["ed_visits_counted"] == 1


@pytest.mark.anyio
async def test_count_prior_ed_visits_window(client, make_user, create_patient, db):
    """Six months back from admission, emergencies only, cancelled admissions ignored."""
    pid, enc, _ = await _stay(client, make_user, create_patient)
    db.expire_all()
    current = await db.get(Encounter, enc)
    patient = current.patient
    base = current.admitted_at
    for days_before, kind, status in ((10, "class", "discharged"), (100, "type", "discharged"),
                                      (181, "class", "discharged"), (183, "class", "discharged"),
                                      (20, "class", "cancelled"), (30, "none", "discharged")):
        db.add(Encounter(patient=patient, patient_id=pid, status=status, admitted_at=base - timedelta(days=days_before),
                         discharged_at=base - timedelta(days=days_before - 1),
                         patient_class="E" if kind == "class" else "I",
                         admission_type="E" if kind == "type" else "C"))
    await db.flush()
    assert await risk.count_prior_ed_visits(db, current) == 3
