"""Merging two patient records that turn out to be one person (HL7 A40, ADR-002 item 11).

Everything about the non-surviving record moves to the survivor: its stays (encounters), discharge workflows, tasks,
summaries, claims and break-glass grants (the HL7 feed moves its own message records). Encrypted fields are bound to
the owning patient's data key (and their row id as associated data), so encounter visit numbers and summary texts are
decrypted with the old patient's key and re-encrypted with the survivor's. The non-surviving row stays for audit
references, with `merged_into` set and its MRN blind index retired, so the old MRN no longer matches anyone.
"""
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AIGeneration, DischargeSummary, DischargeSummaryAddendum, DischargeSummaryVersion
from app.discharge.models import ACTIVE_WORKFLOW_STATUSES, Task, Workflow
from app.insurance.models import InsuranceClaim
from app.patients.models import AccessGrant, Encounter, Patient


async def merge_into(db: AsyncSession, prior: Patient, survivor: Patient) -> dict[str, int]:
    """Move `prior`'s records to `survivor`. Raises HTTPException(409) when the two can't be merged automatically;
    nothing has changed by then. Returns how many rows of each kind moved (ids and counts only)."""
    if prior.gdpr_erased or survivor.gdpr_erased:
        raise HTTPException(status_code=409, detail="An erased record cannot be merged")
    encounters = list(await db.scalars(select(Encounter).where(Encounter.patient_id == prior.patient_id)))
    survivor_active = await db.scalar(select(Encounter.encounter_id).where(
        Encounter.patient_id == survivor.patient_id, Encounter.status == "active"))
    prior_active = next((e for e in encounters if e.status == "active"), None)
    if prior_active is not None and survivor_active is not None:
        raise HTTPException(status_code=409, detail="Both records have a stay in progress; merge them by hand")
    active_workflows = await db.scalar(select(func.count()).select_from(Workflow).where(
        Workflow.patient_id.in_((prior.patient_id, survivor.patient_id)),
        Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES)))
    if (active_workflows or 0) > 1:
        raise HTTPException(status_code=409, detail="Both records have a discharge in progress; merge them by hand")

    for encounter in encounters:
        visit_number = encounter.visit_number  # decrypted with the old patient's key
        encounter.patient = survivor
        encounter.patient_id = survivor.patient_id
        encounter.visit_number = visit_number  # re-encrypted with the survivor's

    # Summaries, their signed versions and addenda (ADR-002 item 13) all hold text under the patient's key.
    keyed_models: tuple[type[DischargeSummary] | type[DischargeSummaryVersion] | type[DischargeSummaryAddendum], ...] = (
        DischargeSummary, DischargeSummaryVersion, DischargeSummaryAddendum)
    summaries = 0
    for keyed in keyed_models:
        for row in list(await db.scalars(select(keyed).where(keyed.patient_id == prior.patient_id))):
            row.rekey(survivor)  # type: ignore[attr-defined]  # select(keyed) is typed as Base
            summaries += keyed is DischargeSummary

    moved = {"encounters": len(encounters), "summaries": summaries}
    for name, model in (("workflows", Workflow), ("tasks", Task), ("claims", InsuranceClaim),
                        ("grants", AccessGrant)):
        result = await db.execute(update(model).where(model.patient_id == prior.patient_id)
                                  .values(patient_id=survivor.patient_id).execution_options(synchronize_session=False))
        moved[name] = result.rowcount or 0  # type: ignore[attr-defined]
    await db.execute(update(AIGeneration).where(AIGeneration.patient_id == prior.patient_id)
                     .values(patient_id=survivor.patient_id).execution_options(synchronize_session=False))

    if prior_active is not None:
        # The stay in progress is now the survivor's, so the survivor mirrors it (status, location, admission).
        for field in ("status", "admission_date", "room_number", "unit_id", "attending_physician"):
            setattr(survivor, field, getattr(prior, field))
    prior.merged_into = survivor.patient_id
    prior.status = "discharged"
    prior.mrn_index = None  # retired: the old MRN no longer finds anyone
    await db.flush()
    return moved
