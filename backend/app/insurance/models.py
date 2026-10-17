from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id

CodeList = JSON().with_variant(JSONB(), "postgresql")
# Numeric, not float, so stored money is exact. asdecimal=False hands floats back to the API layer.
Money = Numeric(12, 2, asdecimal=False)


class InsuranceClaim(Base):
    __tablename__ = "insurance_claims"

    claim_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("clm"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    insurer_name: Mapped[str] = mapped_column(String(200), index=True)
    policy_number: Mapped[str] = mapped_column(String(100), default="")
    diagnosis_codes: Mapped[list] = mapped_column(CodeList, default=list)
    procedure_codes: Mapped[list] = mapped_column(CodeList, default=list)
    billed_amount: Mapped[float] = mapped_column(Money)
    allowed_amount: Mapped[Optional[float]] = mapped_column(Money)
    paid_amount: Mapped[Optional[float]] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
