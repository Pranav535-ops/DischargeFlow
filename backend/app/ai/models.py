from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id


class DischargeSummary(Base):
    __tablename__ = "discharge_summaries"

    summary_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sum"))
    # One summary per workflow, enforced by the unique constraint.
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), unique=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(20), default="ai")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[Optional[str]] = mapped_column(String(40))
    approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
