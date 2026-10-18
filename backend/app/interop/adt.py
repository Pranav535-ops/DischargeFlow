"""Inbound HL7 v2 ADT: A01 (admit) creates or readmits a patient, A03 (discharge) completes the discharge workflow.

Patients are matched by MRN (PID-3) through the keyed blind index on `patients.mrn_index`, since the MRN itself is
encrypted. A03 goes through the same state machine as the UI: when open tasks or an unapproved summary stop
`complete_discharge`, nothing changes, the ACK is AE with the reason, and physicians are notified that the
hospital's ADT system already considers the patient discharged.

ACK texts and audit details carry reasons and ids only, never patient data.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.security import CurrentUser
from app.discharge.service import active_workflow_for, complete_discharge
from app.interop.hl7 import HL7Error, Message, Segment, parse_date
from app.notifications.service import notify_role
from app.patients.models import MRN_INDEX_PURPOSE, Patient, mrn_index_parts

SUPPORTED_EVENTS = ("A01", "A03")
# Stored when an A01 leaves a required column empty, so nobody mistakes it for clinical information.
NOT_PROVIDED = "Not provided by ADT"


@dataclass
class Outcome:
    ack_code: str
    text: str
    error_code: Optional[str] = None
    patient_id: Optional[str] = None


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


def admission_fields(message: Message, pid: Segment) -> dict:
    name = _join(pid.component(5, 2), pid.component(5, 3), pid.component(5, 1))  # given middle family
    if not name:
        raise _ae("101", "PID-5 patient name is required")
    dob = parse_date(pid.component(7))
    if not dob:
        raise _ae("101" if not pid.component(7) else "102", "PID-7 date of birth is missing or not a valid date")

    pv1 = message.segment("PV1")
    room = attending = ""
    admitted = None
    if pv1:
        room_part, bed = pv1.component(3, 2), pv1.component(3, 3)
        room = (f"{room_part}-{bed}" if room_part and bed else room_part) or pv1.component(3, 1)
        attending = _join(pv1.component(7, 6), pv1.component(7, 3), pv1.component(7, 2))  # prefix given family
        admitted = parse_date(pv1.component(44))
    evn = message.segment("EVN")
    admitted = admitted or (parse_date(evn.component(2)) if evn else None) or parse_date(message.msh.component(7))

    dg1, pv2 = message.segment("DG1"), message.segment("PV2")
    diagnosis = ((dg1.component(3, 2) or dg1.component(3, 1)) if dg1 else "") or \
                ((pv2.component(3, 2) or pv2.component(3, 1)) if pv2 else "")
    return {
        "name": name,
        "date_of_birth": dob,
        "admission_date": admitted or NOT_PROVIDED,
        "room_number": room or NOT_PROVIDED,
        "attending_physician": attending or NOT_PROVIDED,
        "diagnosis": diagnosis or NOT_PROVIDED,
    }


async def find_patient(db: AsyncSession, tenant_id: str, authority: Optional[str], mrn: str) -> Optional[Patient]:
    candidates = crypto.blind_index_candidates(MRN_INDEX_PURPOSE, *mrn_index_parts(tenant_id, authority, mrn))
    return await db.scalar(select(Patient).where(Patient.mrn_index.in_(candidates)))


def _pid(message: Message) -> Segment:
    pid = message.segment("PID")
    if pid is None:
        raise _ae("100", "PID segment is required")
    return pid


async def admit(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    pid = _pid(message)
    authority, mrn = mrn_from(pid)
    fields = admission_fields(message, pid)

    patient = await find_patient(db, client.tenant_id, authority, mrn)
    if patient is not None:
        if patient.status != "discharged" or await active_workflow_for(db, patient.patient_id):
            raise _ae("205", "Patient with this MRN is already admitted")
        for key, value in fields.items():
            setattr(patient, key, value)
        patient.status = "admitted"
        patient.set_mrn(client.tenant_id, authority, mrn)  # re-keys a pre-rotation index
        await db.flush()
        return Outcome("AA", "Patient readmitted", patient_id=patient.patient_id)

    patient = Patient(**fields, status="admitted")
    patient.set_mrn(client.tenant_id, authority, mrn)
    db.add(patient)
    try:
        await db.flush()
    except IntegrityError:
        # Another message admitted the same MRN concurrently. Fail the request so the sender retries and gets AE.
        raise HTTPException(status_code=409, detail="Concurrent admission for this MRN; retry")
    return Outcome("AA", "Patient admitted", patient_id=patient.patient_id)


async def discharge(db: AsyncSession, client: CurrentUser, message: Message) -> Outcome:
    pid = _pid(message)
    authority, mrn = mrn_from(pid)
    patient = await find_patient(db, client.tenant_id, authority, mrn)
    if patient is None:
        raise _ae("204", "No patient with this MRN")

    workflow = await active_workflow_for(db, patient.patient_id)
    if workflow is None:
        if patient.status == "discharged":
            return Outcome("AA", "Patient already discharged", patient_id=patient.patient_id)
        reason = "No discharge workflow has been started"
        await _notify_not_applied(db, patient, reason, f"/patients/{patient.patient_id}")
        return Outcome("AE", reason, "207", patient.patient_id)

    try:
        await complete_discharge(db, workflow.workflow_id, client)
    except HTTPException as exc:
        # The state machine refused before changing anything (open tasks, unapproved summary).
        reason = str(exc.detail)
        await _notify_not_applied(db, patient, reason, f"/workflows/{workflow.workflow_id}")
        return Outcome("AE", reason, "207", patient.patient_id)
    return Outcome("AA", "Discharge completed", patient_id=patient.patient_id)


async def _notify_not_applied(db: AsyncSession, patient: Patient, reason: str, link: str) -> None:
    await notify_role(db, "physician", "ADT Discharge Not Applied",
                      f"The hospital ADT system reported {patient.name} as discharged, but DischargeFlow could not "
                      f"complete the discharge: {reason}.", link)


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
        if message.trigger_event == "A01":
            return await admit(db, client, message)
        return await discharge(db, client, message)
    except HL7Error as exc:
        return Outcome(exc.ack, exc.text, exc.code)
