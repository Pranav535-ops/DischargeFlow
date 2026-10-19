"""The bed-to-cleaning handoff (ADR-002 item 17): a bed becomes dirty when a stay ends in it.

Called wherever a stay closes or moves: discharge completion in the UI, the ADT feed's A03 (with or without a
workflow, including reconciliation to `externally_discharged`), a manual status change to discharged, and an A02
transfer out of a room. Kept free of other feature imports so the discharge, patients and interop modules can call it.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capacity.models import OPEN_BED_STATUSES, BedTurnover
from app.core.db import utcnow


async def open_turnover(db: AsyncSession, unit_id: Optional[str], room_number: Optional[str]) -> Optional[BedTurnover]:
    return await db.scalar(select(BedTurnover).where(
        BedTurnover.unit_id.is_(None) if unit_id is None else BedTurnover.unit_id == unit_id,
        BedTurnover.room_number == room_number, BedTurnover.status.in_(OPEN_BED_STATUSES)))


async def mark_dirty(db: AsyncSession, unit_id: Optional[str], room_number: Optional[str],
                     encounter_id: Optional[str] = None, source: str = "discharge") -> Optional[BedTurnover]:
    """Open a turnover for the bed, unless it has no room (nothing to clean that we know of) or one is already open
    (the bed was never made available again: keep the earlier one and its time)."""
    room = (room_number or "").strip()
    if not room:
        return None
    existing = await open_turnover(db, unit_id, room)
    if existing is not None:
        return existing
    turnover = BedTurnover(unit_id=unit_id, room_number=room, encounter_id=encounter_id, source=source,
                           dirty_at=utcnow())
    db.add(turnover)
    await db.flush()
    return turnover


async def bed_vacated(db: AsyncSession, encounter, source: str = "discharge") -> Optional[BedTurnover]:
    """The stay `encounter` (patients.models.Encounter) has left its bed."""
    return await mark_dirty(db, encounter.unit_id, encounter.room_number, encounter.encounter_id, source)


async def departure_cancelled(db: AsyncSession, encounter_id: str) -> None:
    """HL7 A13: the patient is back in the bed. A turnover nobody has started is cancelled; one already being
    cleaned is left for EVS to finish. Beds the stay left earlier by transfer still need cleaning."""
    for turnover in await db.scalars(select(BedTurnover).where(BedTurnover.encounter_id == encounter_id,
                                                               BedTurnover.source == "discharge",
                                                               BedTurnover.status == "dirty")):
        turnover.status = "cancelled"
