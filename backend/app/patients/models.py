from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id

PATIENT_STATUSES = ("admitted", "pending_discharge", "discharged")


class Patient(Base):
    __tablename__ = "patients"

    patient_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pat"))
    name: Mapped[str] = mapped_column(String(200), index=True)
    # Free text for now: the UI and chatbot accept partial values ("Unknown") and erasure writes
    # "[REDACTED]". Phase 2 moves these to typed, encrypted columns.
    date_of_birth: Mapped[str] = mapped_column(String(40))
    admission_date: Mapped[str] = mapped_column(String(40), index=True)
    room_number: Mapped[str] = mapped_column(String(40))
    diagnosis: Mapped[str] = mapped_column(String(1000))
    attending_physician: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="admitted", index=True)
    gdpr_consent: Mapped[Optional[bool]] = mapped_column(Boolean)
    gdpr_erased: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
