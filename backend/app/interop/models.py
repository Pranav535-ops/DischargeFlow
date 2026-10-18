from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin


class HL7Message(TenantMixin, Base):
    """One inbound HL7 message and the ACK it got. Never the message itself: it is full of PHI.

    The unique (client, MSH-10 control id) pair makes retries idempotent: a resent message gets its original ACK
    back instead of being applied twice.
    """

    __tablename__ = "hl7_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", "control_id", name="uq_hl7_messages_client_control"),
    )

    message_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("hl7", 16))
    client_id: Mapped[str] = mapped_column(
        ForeignKey("integration_clients.client_id", ondelete="RESTRICT"), index=True
    )
    control_id: Mapped[str] = mapped_column(String(199))
    message_type: Mapped[str] = mapped_column(String(20))  # e.g. "ADT^A01"
    ack_code: Mapped[str] = mapped_column(String(2))  # AA, AE or AR
    error_code: Mapped[Optional[str]] = mapped_column(String(4))  # HL7 table 0357
    ack_text: Mapped[Optional[str]] = mapped_column(String(500))  # the reason, never patient data
    patient_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("patients.patient_id", ondelete="SET NULL"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
