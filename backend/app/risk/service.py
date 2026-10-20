"""Readmission risk on the discharge plan (ADR-002 item 20): the LACE score of a stay, its inputs and its view.

The score is computed by app.risk.lace (pure) from the encounter's own fields; `Encounter.rescore` stores it. This
module adds what needs the database: counting the patient's prior emergency visits in this system, recording the
inputs staff set on the plan, and the explained view the plan screen shows. Reads happen inside the plan reads,
which already write one PHI_READ audit entry per request. Nothing here is sent to a model.

Recomputed when an input changes: a stay opens (A01, a new patient), the expected discharge date or the admission
time changes, staff edit the risk inputs, records merge (A40), and the stay closes or reopens (actual length of stay).
A score from an older SCORE_VERSION, or one never computed, is recomputed when the plan is next read; so is the
"length of stay so far" of a stay without a future expected date, which grows by a day each day.
"""
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.patients.models import Encounter
from app.risk import lace

# HL7 v2 table 0004 patient class (PV1-2): an emergency encounter counts as an ED visit.
EMERGENCY_CLASS = "E"
INPUT_FIELDS = ("admission_type", "patient_class", "comorbidities", "ed_visits_recorded")

# Offered (never added automatically) when a stay is high risk. Keys are stable; the plan screen lists those not yet
# added. Due times are hours from 00:00 on the expected discharge date (hospital time), as in workflow templates.
SUGGESTED_TASKS: list[dict] = [
    {"key": "follow_up_7_days", "title": "Follow-up Appointment Within 7 Days",
     "description": "High readmission risk: book a follow-up with the primary care physician or specialist within "
                    "7 days of discharge, and give the patient the date before they leave",
     "category": "follow_up", "assigned_role": "nurse", "priority": 1, "due_offset_hours": 10},
    {"key": "medication_teach_back", "title": "Medication Teach-back",
     "description": "High readmission risk: go through each discharge medication with the patient or caregiver "
                    "and confirm understanding by teach-back",
     "category": "medication", "assigned_role": "pharmacist", "priority": 2, "due_offset_hours": 10},
    {"key": "post_discharge_call", "title": "Post-discharge Phone Call Within 72 Hours",
     "description": "High readmission risk: arrange a call to the patient within 72 hours of discharge to check "
                    "symptoms, medications and the follow-up appointment",
     "category": "follow_up", "assigned_role": "nurse", "priority": 2, "due_offset_hours": 11},
]
SUGGESTED_KEYS = tuple(t["key"] for t in SUGGESTED_TASKS)

_BASIS = {
    "actual": "actual stay",
    "expected": "to the expected discharge date",
    "so_far": "so far (no expected discharge date in the future)",
    "unknown": "admission time unknown",
}
_COMPONENT_LABELS = {"length_of_stay": ("L", "Length of stay", 7), "acuity": ("A", "Acuity of admission", 3),
                     "comorbidity": ("C", "Comorbidity (Charlson index)", 5),
                     "ed_visits": ("E", "Emergency visits in the 6 months before admission", 4)}


async def count_prior_ed_visits(db: AsyncSession, encounter: Encounter) -> int:
    """The patient's emergency encounters in this system in the ED_LOOKBACK_DAYS before this admission: patient
    class E (PV1-2), or a stay admitted as an emergency (admission type E), which came through the ED. Cancelled
    admissions don't count. ED visits that never reached this system (no ADT message) are missed: staff record those
    as `ed_visits_recorded`."""
    anchor = encounter.admitted_at or utcnow()
    return int(await db.scalar(select(func.count(Encounter.encounter_id)).where(
        Encounter.patient_id == encounter.patient_id, Encounter.encounter_id != encounter.encounter_id,
        Encounter.status != "cancelled", Encounter.admitted_at.is_not(None),
        Encounter.admitted_at >= anchor - timedelta(days=lace.ED_LOOKBACK_DAYS), Encounter.admitted_at < anchor,
        or_(Encounter.patient_class == EMERGENCY_CLASS, Encounter.admission_type == "E"))) or 0)


async def recompute(db: AsyncSession, encounter: Encounter) -> lace.LaceScore:
    return encounter.rescore(await count_prior_ed_visits(db, encounter))


async def ensure_current(db: AsyncSession, encounter: Encounter) -> None:
    """Before a read: compute a score that is missing or from an older version; refresh the length of stay of a
    stay still in (a no-op write unless it changed)."""
    if encounter.readmission_score_detail is None or encounter.readmission_score_version != lace.SCORE_VERSION:
        await recompute(db, encounter)
    elif encounter.status == "active":
        encounter.rescore()


def _explain(key: str, part: dict, comorbidities: Optional[list[str]]) -> str:
    if key == "length_of_stay":
        days = part.get("days")
        return (f"{days} day{'' if days == 1 else 's'} {_BASIS.get(part.get('basis') or '', '')}".strip()
                if days is not None else "Not known: the admission time is missing")
    if key == "acuity":
        code = part.get("admission_type")
        if code is None:
            return "Not recorded: set the admission type (HL7 PV1-4)"
        label = lace.ADMISSION_TYPES.get(code, code)
        return f"{label} admission: {'unplanned (acute)' if lace.is_acute(code) else 'planned'}"
    if key == "comorbidity":
        if comorbidities is None:
            return "Not recorded: record the patient's Charlson comorbidities"
        counted = lace.counted_comorbidities(comorbidities)
        if not counted:
            return "Charlson index 0: none of the listed conditions"
        listed = ", ".join(f"{lace.COMORBIDITIES[k][0]} {lace.COMORBIDITIES[k][1]}" for k in counted)
        return f"Charlson index {part.get('charlson_index')}: {listed}"
    visits = part.get("visits")
    if visits is None:
        return "Not counted yet"
    source = ("recorded by staff from the hospital's records" if part.get("source") == "recorded"
              else "counted from this system's encounters")
    capped = " (4 points at most)" if visits > lace.ED_POINTS_CAP else ""
    return f"{visits} visit{'' if visits == 1 else 's'}, {source}{capped}"


def view(encounter: Encounter) -> Optional[dict]:
    """The score with each component explained, the inputs (for the edit form) and, when high risk, the suggested
    tasks. None if the stay was never scored."""
    detail = encounter.readmission_score_detail
    if detail is None or encounter.readmission_score is None:
        return None
    comorbidities = encounter.comorbidities
    components = []
    for key, (letter, label, max_points) in _COMPONENT_LABELS.items():
        part = detail.get(key, {})
        components.append({"key": key, "letter": letter, "label": label, "points": part.get("points", 0),
                           "max_points": max_points, "explanation": _explain(key, part, comorbidities)})
    score = encounter.readmission_score
    return {
        "score": score,
        "max_score": lace.MAX_SCORE,
        "high_risk": lace.is_high_risk(score),
        "threshold": lace.HIGH_RISK_THRESHOLD,
        "complete": not detail.get("missing"),
        "missing": detail.get("missing", []),
        "version": encounter.readmission_score_version,
        "computed_at": encounter.readmission_scored_at.isoformat() if encounter.readmission_scored_at else None,
        "method": "LACE index",
        "citation": lace.CITATION,
        "components": components,
        "inputs": {"admission_type": encounter.admission_type, "patient_class": encounter.patient_class,
                   "comorbidities": comorbidities, "ed_visits_recorded": encounter.ed_visits_recorded,
                   "ed_visits_counted": detail.get("ed_visits_counted")},
    }


def summary(encounter: Encounter) -> Optional[dict]:
    """The compact form for lists (the daily plan review)."""
    if encounter.readmission_score is None:
        return None
    return {"score": encounter.readmission_score, "high_risk": lace.is_high_risk(encounter.readmission_score),
            "complete": not (encounter.readmission_score_detail or {}).get("missing")}


async def update_inputs(db: AsyncSession, user: CurrentUser, encounter: Encounter, changes: dict,
                        request: Optional[Request] = None) -> lace.LaceScore:
    """Set the risk inputs staff record on the plan (keys of `changes`, None clears one) and recompute. The audit
    entry names the fields that changed, never their values (the conditions are health information)."""
    if encounter.status != "active":
        raise HTTPException(status_code=400, detail="The patient has already been discharged from this stay")
    unknown = lace.unknown_comorbidities(changes.get("comorbidities") or [])
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown comorbidities: {', '.join(unknown)}")
    changed = []
    for field in INPUT_FIELDS:
        if field not in changes:
            continue
        value = changes[field]
        if field == "comorbidities" and value is not None:
            value = sorted(set(value))
        if value != getattr(encounter, field):
            setattr(encounter, field, value)
            changed.append(field)
    result = await recompute(db, encounter)
    await db.flush()
    await write_audit(db, user, "UPDATE_RISK_INPUTS", "patient", encounter.patient_id,
                      f"encounter={encounter.encounter_id} fields: {', '.join(changed) or 'none'} "
                      f"version={result.version}", request)
    return result
