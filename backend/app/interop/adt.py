"""Inbound HL7 v2 ADT: admit, transfer, discharge, update, cancellations and merges (ADR-001 item 24, ADR-002 item 11).

Patients are matched by MRN (PID-3) through the keyed blind index on `patients.mrn_index`, since the MRN itself is
encrypted. Each A01 opens an encounter (one stay, with the visit number from PV1-19); a readmission is a new
encounter, so earlier stays are kept.

- A01 admit: a new patient, or a readmission of a discharged one.
- A02 transfer: the new unit and room/bed (PV1-3) on the patient and the current encounter.
- A03 discharge: completes the discharge workflow through the state machine when it is ready. When it is not (open
  tasks, unapproved summary), the A03 is still accepted (AA): the patient has left, so the stay closes and the
  workflow becomes `externally_discharged`, with the departure time and a policy deadline for the tasks still open.
  Physicians and the unit's nurses are told. With no workflow at all, the stay simply closes.
- A08 update: only whitelisted fields (UPDATABLE_FIELDS), and only those the message carries; PV1-4 admission type
  and PV1-2 patient class update the stay in progress (readmission risk inputs, ADR-002 item 20).
- A11 cancel admit: the current encounter is marked `cancelled`.
- A13 cancel discharge: the last stay reopens, and an externally discharged workflow goes back to in progress.
- A40 merge: the MRG-1 record's stays and everything else move to the PID-3 (surviving) record, and the old MRN's
  blind index is retired (patients/merge.py).

Events for an MRN not seen yet (an A03 that overtook its A01) are parked, encrypted, and replayed when the admission
arrives (interop/parking.py). ACK texts and audit details carry reasons and ids only, never patient data.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core import crypto
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import EXTERNALLY_DISCHARGED, Workflow
from app.discharge.service import (active_workflow_for, complete_discharge, record_external_discharge,
                                   reopen_external_discharge)
from app.identity.models import Unit
from app.interop import parking
from app.interop.hl7 import HL7Error, Message, Segment, parse_date, parse_datetime
from app.interop.models import HL7Message
from app.notifications.service import notify_role
from app.patients.merge import merge_into
from app.patients.models import MRN_INDEX_PURPOSE, Encounter, Patient, mrn_index_parts
from app.patients.service import current_encounter, open_encounter
from app.risk import service as risk
from app.risk.lace import ADMISSION_TYPES

SUPPORTED_EVENTS = ("A01", "A02", "A03", "A08", "A11", "A13", "A40")
# Stored when an A01 leaves a required column empty, so nobody mistakes it for clinical information.
NOT_PROVIDED = "Not provided by ADT"
# What an A08 may change. Location changes come with A02, status changes with A01/A03/A11/A13.
UPDATABLE_FIELDS = ("name", "date_of_birth", "attending_physician", "diagnosis")
# A departure time later than now (sender clock skew) is recorded as now.
CLOCK_SKEW = timedelta(minutes=5)


@dataclass
class Outcome:
    ack_code: str
    text: str
    error_code: Optional[str] = None
    patient_id: Optional[str] = None
    # Set when the message waits for an MRN not seen yet: (assigning authority, MRN).
    park: Optional[tuple[Optional[str], str]] = None
    # Set when the message brought an MRN in, so messages parked for it can be replayed.
    replay: Optional[tuple[Optional[str], str]] = None


class _Park(Exception):
    def __init__(self, authority: Optional[str], mrn: str):
        self.authority, self.mrn = authority, mrn


def _ae(code: str, text: str) -> HL7Error:
    return HL7Error(code, text, ack="AE")


def mrn_from(pid: Segment) -> tuple[Optional[str], str]:
    """(assigning authority, MRN) from PID-3: the repetition typed MR, else the first one with an id."""
    reps = pid.repetitions(3)
    chosen = next((r for r in reps if pid.component_of(r, 5).upper() == "MR" and pid.component_of(r, 1)), None)
    if chosen is None:
        chosen = next((r for r in reps if pid.component_of(r, 1)), None)
    if chosen is None:
        raise _ae("101", "PID-3 patient identifier (MRN) is required")
    return pid.component_of(chosen, 4) or None, pid.component_of(chosen, 1)


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _location(pv1: Optional[Segment]) -> str:
    """Room (and bed) from PV1-3 (point of care^room^bed)."""
    if pv1 is None:
        return ""
    room_part, bed = pv1.component(3, 2), pv1.component(3, 3)
    return (f"{room_part}-{bed}" if room_part and bed else room_part) or pv1.component(3, 1)


def _attending(pv1: Optional[Segment]) -> str:
    return _join(pv1.component(7, 6), pv1.component(7, 3), pv1.component(7, 2)) if pv1 else ""  # prefix given family


def _diagnosis(message: Message) -> str:
    dg1, pv2 = message.segment("DG1"), message.segment("PV2")
    return ((dg1.component(3, 2) or dg1.component(3, 1)) if dg1 else "") or \
           ((pv2.component(3, 2) or pv2.component(3, 1)) if pv2 else "")


def _name(pid: Segment) -> str:
    return _join(pid.component(5, 2), pid.component(5, 3), pid.component(5, 1))  # given middle family


def admission_fields(message: Message, pid: Segment) -> dict:
    name = _name(pid)
    if not name:
        raise _ae("101", "PID-5 patient name is required")
    dob = parse_date(pid.component(7))
    if not dob:
        raise _ae("101" if not pid.component(7) else "102", "PID-7 date of birth is missing or not a valid date")

    pv1 = message.segment("PV1")
    admitted = parse_date(pv1.component(44)) if pv1 else None
    evn = message.segment("EVN")
    admitted = admitted or (parse_date(evn.component(2)) if evn else None) or parse_date(message.msh.component(7))
    return {
        "name": name,
        "date_of_birth": dob,
        "admission_date": admitted or NOT_PROVIDED,
        "room_number": _location(pv1) or NOT_PROVIDED,
        "attending_physician": _attending(pv1) or NOT_PROVIDED,
        "diagnosis": _diagnosis(message) or NOT_PROVIDED,
    }


async def find_patient(db: AsyncSession, tenant_id: str, authority: Optional[str], mrn: str) -> Optional[Patient]:
    candidates = crypto.blind_index_candidates(MRN_INDEX_PURPOSE, *mrn_index_parts(tenant_id, authority, mrn))
    return await db.scalar(select(Patient).where(Patient.mrn_index.in_(candidates)))


def _pid(message: Message) -> Segment:
    pid = message.segment("PID")
    if pid is None:
        raise _ae("100", "PID segment is required")
    return pid


async def _known_patient(db: AsyncSession, client: CurrentUser, message: Message) -> Patient:
    """The patient the message is about; an MRN not seen yet parks the message until its admission arrives."""
    authority, mrn = mrn_from(_pid(message))
    patient = await find_patient(db, client.tenant_id, authority, mrn)
    if patient is None:
        raise _Park(authority, mrn)
    return patient


async def unit_for(db: AsyncSession, message: Message) -> Optional[str]:
    """The unit whose code is the point of care in PV1-3.1, if one is set up (ADR-002 item 3)."""
    pv1 = message.segment("PV1")
    code = pv1.component(3, 1).strip().upper() if pv1 else ""
    return await db.scalar(select(Unit.unit_id).where(Unit.code == code)) if code else None


def visit_number_from(message: Message) -> Optional[str]:
    """PV1-19, the visit (account) number the hospital gives this stay."""
    pv1 = message.segment("PV1")
    return (pv1.component(19).strip() or None) if pv1 else None


def acuity_from(message: Message) -> dict:
    """PV1-4 admission type (HL7 table 0007) and PV1-2 patient class (table 0004), the readmission risk score's
    acuity and ED-visit inputs (ADR-002 item 20). Only the codes the message carries; unknown admission types are
    dropped rather than guessed."""
    pv1 = message.segment("PV1")
    if pv1 is None:
        return {}
    out = {}
    admission_type = pv1.component(4).strip().upper()
    if admission_type in ADMISSION_TYPES:
        out["admission_type"] = admission_type
    patient_class = pv1.component(2).strip().upper()[:2]
    if patient_class:
        out["patient_class"] = patient_class
    return out


def disposition_from(message: Message) -> Optional[str]:
    """PV1-36 discharge disposition, as the sending system codes it (HL7 table 0112, e.g. 01 home)."""
    pv1 = message.segment("PV1")
    return (pv1.component(36).strip()[:40] or None) if pv1 else None


def departure_from(message: Message) -> datetime:
    """When the patient left: PV1-45 discharge time, else EVN-6 (event occurred), EVN-2 (recorded), MSH-7, now."""
    pv1, evn = message.segment("PV1"), message.segment("EVN")
    for value in (pv1.component(45) if pv1 else "", evn.component(6) if evn else "", evn.component(2) if evn else "",
                  message.msh.component(7)):
        when = parse_datetime(value) if value else None
        if when is not None:
            return utcnow() if when > utcnow() + CLOCK_SKEW else when
    return utcnow()


def _check_visit(message: Message, encounter: Optional[Encounter]) -> None:
    visit_number = visit_number_from(message)
    if visit_number and encounter is not None and encounter.visit_number not in (None, visit_number):
        # Changing the wrong stay would be worse than changing none.
        raise _ae("207", "PV1-19 visit number does not match the patient's encounter")


async def _visit_seen(db: AsyncSession, patient_id: str, visit_number: str) -> bool:
    encounters = await db.scalars(select(Encounter).where(Encounter.patient_id == patient_id,
                                                          Encounter.status != "cancelled"))
    return any(e.visit_number == visit_number for e in encounters)  # encrypted: compared after decrypting


async def _notify(db: AsyncSession, patient: Patient, title: str, message: str, link: str,
                  roles: tuple[str, ...] = ("physician", "nurse")) -> None:
    for role in roles:
        await notify_role(db, role, title, message, link, unit_id=patient.unit_id)


# ---------- A01 ----------

async def admit(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    pid = _pid(message)
    authority, mrn = mrn_from(pid)
    fields = admission_fields(message, pid)
    fields["unit_id"] = await unit_for(db, message)
    visit_number = visit_number_from(message)

    patient = await find_patient(db, client.tenant_id, authority, mrn)
    if patient is not None:
        if patient.status != "discharged" or await active_workflow_for(db, patient.patient_id):
            raise _ae("205", "Patient with this MRN is already admitted")
        if visit_number and await _visit_seen(db, patient.patient_id, visit_number):
            # An old A01 for a stay that has already ended (resent under a new control id): never reopen it.
            raise _ae("205", "This visit number has already been admitted")
        stale = await current_encounter(db, patient.patient_id)
        if stale is not None:  # discharged by hand without closing the stay (should not happen; keep one active)
            stale.close(stale.discharged_at or stale.updated_at)
        for key, value in fields.items():
            setattr(patient, key, value)
        patient.status = "admitted"
        patient.set_mrn(client.tenant_id, authority, mrn)  # re-keys a pre-rotation index
        await db.flush()
        await open_encounter(db, patient, visit_number, **acuity_from(message))
        return Outcome("AA", "Patient readmitted", patient_id=patient.patient_id, replay=(authority, mrn))

    patient = Patient(**fields, status="admitted")
    patient.set_mrn(client.tenant_id, authority, mrn)
    db.add(patient)
    try:
        await db.flush()
    except IntegrityError:
        # Another message admitted the same MRN concurrently. Fail the request so the sender retries and gets AE.
        raise HTTPException(status_code=409, detail="Concurrent admission for this MRN; retry")
    await open_encounter(db, patient, visit_number, **acuity_from(message))
    return Outcome("AA", "Patient admitted", patient_id=patient.patient_id, replay=(authority, mrn))


# ---------- A02 ----------

async def transfer(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    patient = await _known_patient(db, client, message)
    encounter = await current_encounter(db, patient.patient_id)
    if patient.status == "discharged" or encounter is None:
        raise _ae("207", "Patient is not admitted")
    _check_visit(message, encounter)
    room = _location(message.segment("PV1"))
    if not room:
        raise _ae("101", "PV1-3 assigned location is required")
    unit_id = await unit_for(db, message)
    patient.room_number = encounter.room_number = room
    patient.unit_id = encounter.unit_id = unit_id
    return Outcome("AA", f"Patient transferred (unit {unit_id or 'unassigned'})", patient_id=patient.patient_id)


# ---------- A03 ----------

async def discharge(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    patient = await _known_patient(db, client, message)
    encounter = await current_encounter(db, patient.patient_id)
    _check_visit(message, encounter)
    disposition = disposition_from(message)

    workflow = await active_workflow_for(db, patient.patient_id)
    if workflow is None:
        if patient.status == "discharged":
            return Outcome("AA", "Patient already discharged", patient_id=patient.patient_id)
        departed = departure_from(message)
        if encounter is not None:
            encounter.close(departed, disposition)
        patient.status = "discharged"
        await _notify(db, patient, "Patient Left Without a Discharge Workflow",
                      f"The hospital ADT system discharged {patient.name}; no discharge workflow had been started.",
                      f"/patients/{patient.patient_id}", roles=("physician",))
        return Outcome("AA", "Discharge recorded; no discharge workflow had been started",
                       patient_id=patient.patient_id)

    try:
        await complete_discharge(db, workflow.workflow_id, client, disposition=disposition)
    except HTTPException as exc:
        # The state machine refused before changing anything (open tasks, unapproved summary). The patient has left
        # all the same: record it and keep the remaining work open (design change 2).
        reason = str(exc.detail)
        await record_external_discharge(db, workflow, client, departure_from(message), disposition, reason)
        return Outcome("AA", f"Discharge recorded; workflow left open ({reason})", patient_id=patient.patient_id)
    return Outcome("AA", "Discharge completed", patient_id=patient.patient_id)


# ---------- A08 ----------

async def update_patient(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    patient = await _known_patient(db, client, message)
    pid, pv1 = _pid(message), message.segment("PV1")
    values = {"name": _name(pid), "attending_physician": _attending(pv1), "diagnosis": _diagnosis(message)}
    if pid.component(7):
        values["date_of_birth"] = parse_date(pid.component(7)) or ""
        if not values["date_of_birth"]:
            raise _ae("102", "PID-7 date of birth is not a valid date")
    changed = [f for f in UPDATABLE_FIELDS if values.get(f) and values[f] != getattr(patient, f)]
    for field in changed:
        setattr(patient, field, values[field])
    encounter = await current_encounter(db, patient.patient_id)
    if encounter is not None:
        # The readmission risk inputs the PV1 carries (ADR-002 item 20), on the stay in progress.
        stay = {k: v for k, v in acuity_from(message).items() if v != getattr(encounter, k)}
        for field, value in stay.items():
            setattr(encounter, field, value)
        if stay:
            changed += sorted(stay)
            await risk.recompute(db, encounter)
    text = f"Patient updated: {', '.join(changed)}" if changed else "No changes"
    return Outcome("AA", text, patient_id=patient.patient_id)


# ---------- A11 ----------

async def cancel_admit(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    patient = await _known_patient(db, client, message)
    encounter = await current_encounter(db, patient.patient_id)
    if encounter is None:
        latest = await db.scalar(select(Encounter).where(Encounter.patient_id == patient.patient_id)
                                 .order_by(Encounter.created_at.desc()).limit(1))
        if latest is not None and latest.status == "cancelled":
            return Outcome("AA", "Admission already cancelled", patient_id=patient.patient_id)
        raise _ae("207", "Patient has no admission in progress")
    _check_visit(message, encounter)
    if await active_workflow_for(db, patient.patient_id):
        reason = "A discharge workflow has been started for this admission"
        await _notify(db, patient, "ADT Cancel Admission Not Applied",
                      f"The hospital ADT system cancelled the admission of {patient.name}, but DischargeFlow kept it: "
                      f"{reason.lower()}.", f"/patients/{patient.patient_id}", roles=("physician", "admin"))
        raise _ae("207", reason)

    encounter.status = "cancelled"
    patient.status = "discharged"
    previous = await db.scalar(select(Encounter).where(
        Encounter.patient_id == patient.patient_id, Encounter.status == "discharged")
        .order_by(Encounter.created_at.desc()).limit(1))
    if previous is not None:  # the patient's mirror fields go back to the last real stay
        patient.room_number = previous.room_number or patient.room_number
        patient.unit_id = previous.unit_id
        if previous.admitted_at is not None:
            patient.admission_date = previous.admitted_at.date().isoformat()
    return Outcome("AA", "Admission cancelled", patient_id=patient.patient_id)


# ---------- A13 ----------

async def cancel_discharge(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    patient = await _known_patient(db, client, message)
    latest = await db.scalar(select(Encounter).where(
        Encounter.patient_id == patient.patient_id, Encounter.status != "cancelled")
        .order_by(Encounter.created_at.desc()).limit(1))
    if patient.status != "discharged" or latest is None or latest.status == "active":
        return Outcome("AA", "Patient is not discharged; nothing to cancel", patient_id=patient.patient_id)
    _check_visit(message, latest)

    latest.reopen()
    patient.status = "admitted"
    patient.room_number = latest.room_number or patient.room_number
    patient.unit_id = latest.unit_id
    text = "Discharge cancelled; the stay is open again"
    workflow = await db.scalar(select(Workflow).where(Workflow.encounter_id == latest.encounter_id)
                               .order_by(Workflow.started_at.desc()).limit(1))
    if workflow is not None and workflow.status == EXTERNALLY_DISCHARGED:
        await reopen_external_discharge(db, workflow, client)
        patient.status = "pending_discharge"
        text += "; discharge workflow back in progress"
    elif workflow is not None and workflow.status == "completed":
        text += "; the completed discharge workflow stays completed"
    await _notify(db, patient, "ADT Discharge Cancelled",
                  f"The hospital ADT system cancelled the discharge of {patient.name}; the stay is open again.",
                  f"/patients/{patient.patient_id}")
    return Outcome("AA", text, patient_id=patient.patient_id)


# ---------- A40 ----------

async def merge(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    authority, mrn = mrn_from(_pid(message))
    mrg = message.segment("MRG")
    if mrg is None:
        raise _ae("100", "MRG segment is required")
    prior_mrn, prior_authority = mrg.component(1, 1), mrg.component(1, 4) or None
    if not prior_mrn:
        raise _ae("101", "MRG-1 prior patient identifier is required")
    if mrn_index_parts(client.tenant_id, prior_authority, prior_mrn) == \
            mrn_index_parts(client.tenant_id, authority, mrn):
        raise _ae("102", "MRG-1 names the surviving MRN itself")

    prior = await find_patient(db, client.tenant_id, prior_authority, prior_mrn)
    survivor = await find_patient(db, client.tenant_id, authority, mrn)
    if prior is None:
        return Outcome("AA", "No patient with the prior MRN; nothing to merge",
                       patient_id=survivor.patient_id if survivor else None)
    if survivor is None:
        # Only the old MRN is known: the record simply takes the surviving MRN, and the old index is retired.
        prior.set_mrn(client.tenant_id, authority, mrn)
        await write_audit(db, client, "MERGE_PATIENT", "patient", prior.patient_id,
                          "MRN replaced by the surviving MRN (HL7 A40); previous MRN index retired")
        return Outcome("AA", "Patient record now carries the surviving MRN", patient_id=prior.patient_id,
                       replay=(authority, mrn))
    try:
        moved = await merge_into(db, prior, survivor)
    except HTTPException as exc:
        await notify_role(db, "admin", "ADT Merge Not Applied",
                          f"An HL7 A40 asked to merge patient {prior.patient_id} into {survivor.patient_id}: "
                          f"{exc.detail}.", f"/patients/{survivor.patient_id}")
        raise _ae("207", str(exc.detail))
    await db.execute(update(HL7Message).where(HL7Message.patient_id == prior.patient_id)
                     .values(patient_id=survivor.patient_id).execution_options(synchronize_session=False))
    counts = " ".join(f"{k}={v}" for k, v in moved.items())
    await write_audit(db, client, "MERGE_PATIENT", "patient", survivor.patient_id,
                      f"merged {prior.patient_id} into this record (HL7 A40): {counts}; prior MRN index retired")
    await write_audit(db, client, "MERGE_PATIENT", "patient", prior.patient_id,
                      f"merged into {survivor.patient_id} (HL7 A40); MRN index retired")
    return Outcome("AA", "Records merged", patient_id=survivor.patient_id)


HANDLERS = {"A01": admit, "A02": transfer, "A03": discharge, "A08": update_patient, "A11": cancel_admit,
            "A13": cancel_discharge, "A40": merge}


async def process(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    """Apply one parsed message. HL7Error from validation becomes an AR/AE outcome; nothing has changed by then."""
    try:
        if message.message_type != "ADT":
            raise HL7Error("200", f"Unsupported message type {message.message_type or '(none)'}; only ADT is accepted")
        if message.trigger_event not in SUPPORTED_EVENTS:
            raise HL7Error("201", f"Unsupported ADT event {message.trigger_event or '(none)'}; "
                                  f"supported: {', '.join(SUPPORTED_EVENTS)}")
        if message.processing_id != "P":
            raise HL7Error("202", "Only production messages (MSH-11 = P) are processed")
        return await HANDLERS[message.trigger_event](db, client, message)
    except _Park as wait:
        return Outcome("AA", parking.PARKED_TEXT, park=(wait.authority, wait.mrn))
    except HL7Error as exc:
        return Outcome(exc.ack, exc.text, exc.code)


async def receive(db: AsyncSession, client: CurrentUser, message: Message, raw: str, record: HL7Message) -> Outcome:
    """Process one received message: expire old parked messages, apply this one, park it if it is early, and replay
    what was parked for an MRN it brought in."""
    await parking.expire_due(db)
    outcome = await process(db, client, message)
    if outcome.park is not None:
        await parking.park(db, client, record, raw, *outcome.park)
    elif outcome.replay is not None:
        async def apply(parked: Message) -> tuple[str, str, Optional[str], bool]:
            result = await process(db, client, parked)
            return result.ack_code, result.text, result.patient_id, result.park is not None

        count = await parking.replay_for(db, client, *outcome.replay, apply=apply)
        if count:
            outcome.text += f"; {count} parked message(s) applied"
    return outcome
