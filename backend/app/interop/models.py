from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, LargeBinary, String, Text, UniqueConstraint
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


PARKED_STATUSES = ("parked", "replayed", "expired", "discarded")


class HL7ParkedMessage(TenantMixin, Base):
    """An ADT message that arrived before its prerequisite (an A03 before the A01 that admits the MRN), kept until
    that arrives (ADR-002 item 11, finding 10).

    The message is full of PHI, so it is stored only as ciphertext under its own data key (wrapped by the KEK, as a
    patient's is), with the parked id as associated data. `mrn_index` is the blind index of the MRN it waits for. Once
    the message is replayed, expires or is discarded, the ciphertext and the key are both dropped.
    """

    __tablename__ = "hl7_parked_messages"
    __api_exclude__ = frozenset({"message", "dek", "mrn_index"})

    parked_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("hlp", 16))
    message_id: Mapped[str] = mapped_column(ForeignKey("hl7_messages.message_id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("integration_clients.client_id", ondelete="RESTRICT"), index=True
    )
    event: Mapped[str] = mapped_column(String(20))  # e.g. "ADT^A03"
    mrn_index: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[Optional[str]] = mapped_column(Text)  # ciphertext ("enc:v1:...") of the ER7 message
    dek: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), default="parked", index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # The outcome of the replay (AA/AE/AR and the reason, never patient data).
    result_ack: Mapped[Optional[str]] = mapped_column(String(2))
    result_text: Mapped[Optional[str]] = mapped_column(String(500))
