from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

# A bed's way back into service after a patient leaves (ADR-002 item 17): dirty -> cleaning -> available.
# `cancelled`: the departure was undone (HL7 A13) before cleaning started.
BED_STATUSES = ("dirty", "cleaning", "available", "cancelled")
OPEN_BED_STATUSES = ("dirty", "cleaning")
_OPEN_SQL = text("status IN ('dirty', 'cleaning')")


class BedTurnover(TenantMixin, Base):
    """One turnover of one bed: opened `dirty` when a stay ends there, advanced by EVS or the charge nurse.

    A bed is identified by its unit and room (the same location strings the encounter carries; there is no bed
    master yet). The encounter is kept to undo a cancelled discharge, but it is never returned by the API: the board
    shows locations and times only. Unit-scoped by `unit_id` (rls.UNIT_SCOPED_LOCATION_TABLES).
    """

    __tablename__ = "bed_turnovers"
    __api_exclude__ = frozenset({"encounter_id"})
    __table_args__ = (
        # At most one open turnover per bed.
        Index("uq_bed_turnovers_one_open_per_bed", "tenant_id", "unit_id", "room_number", unique=True,
              postgresql_where=_OPEN_SQL, sqlite_where=_OPEN_SQL),
    )

    turnover_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("bed"))
    unit_id: Mapped[Optional[str]] = mapped_column(ForeignKey("units.unit_id", ondelete="SET NULL"), index=True)
    room_number: Mapped[str] = mapped_column(String(40))
    encounter_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("encounters.encounter_id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="dirty", index=True)
    # Why it was opened: discharge (UI or ADT A03), transfer (ADT A02) or manual.
    source: Mapped[str] = mapped_column(String(20), default="discharge")
    dirty_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    cleaning_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    cleaning_started_by: Mapped[Optional[str]] = mapped_column(String(40))
    available_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    available_by: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
