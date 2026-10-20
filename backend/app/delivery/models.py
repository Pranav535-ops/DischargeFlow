"""Outbound delivery of the signed discharge summary (ADR-002 item 16).

- `delivery_endpoints`: the directory of receivers (primary care practices, receiving facilities), admin-managed.
  An endpoint says which document it takes (a FHIR R4 document Bundle or a C-CDA Discharge Summary) and how it is
  reached (an HTTPS POST, or the `mock` transport outside production). No PHI.
- `discharge_recipients`: who should get one stay's summary (set on the encounter with the discharge plan).
- `summary_deliveries`: one row per (summary, endpoint, signature or addendum), the outbox the runner works through:
  queued -> sent -> acknowledged, or failed. Errors are short codes and HTTP statuses, never PHI.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

DOCUMENT_FORMATS = ("fhir_bundle", "ccda")
TRANSPORTS = ("https", "mock")
RECIPIENT_KINDS = ("primary_care", "receiving_facility", "other")
# queued: waiting for its (next) attempt. sent: the receiver accepted it for processing (HTTP 202) and has not
# acknowledged it yet. acknowledged: the receiver stored it (HTTP 200/201), or acknowledged it later.
# failed: gave up (permanent refusal or too many attempts) and on the worklist. cancelled: not to be sent.
DELIVERY_STATUSES = ("queued", "sent", "acknowledged", "failed", "cancelled")
OPEN_STATUSES = ("queued",)


class DeliveryEndpoint(TenantMixin, Base):
    __tablename__ = "delivery_endpoints"

    endpoint_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dep"))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="primary_care")
    document_format: Mapped[str] = mapped_column(String(20), default="fhir_bundle")
    transport: Mapped[str] = mapped_column(String(20), default="https")
    # For a FHIR server, its base URL (the Bundle is POSTed to <url>/Bundle); otherwise the exact URL to POST to.
    url: Mapped[Optional[str]] = mapped_column(String(2048))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class DischargeRecipient(TenantMixin, Base):
    """One receiver of one stay's discharge summary. Unit-scoped like the encounter."""

    __tablename__ = "discharge_recipients"
    __table_args__ = (UniqueConstraint("encounter_id", "endpoint_id", name="uq_discharge_recipients_encounter_endpoint"),)

    recipient_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("drc"))
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("delivery_endpoints.endpoint_id", ondelete="CASCADE"))
    added_by: Mapped[str] = mapped_column(String(40))
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SummaryDelivery(TenantMixin, Base):
    __tablename__ = "summary_deliveries"
    __table_args__ = (
        # One delivery per signature (version) or addendum per endpoint: enqueueing twice is a no-op.
        UniqueConstraint("summary_id", "endpoint_id", "trigger_id", name="uq_summary_deliveries_trigger"),
        Index("ix_summary_deliveries_due", "status", "next_attempt_at"),
    )

    delivery_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dlv"))
    summary_id: Mapped[str] = mapped_column(ForeignKey("discharge_summaries.summary_id", ondelete="CASCADE"),
                                            index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(40))
    encounter_id: Mapped[Optional[str]] = mapped_column(String(40))
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("delivery_endpoints.endpoint_id", ondelete="CASCADE"),
                                             index=True)
    # What prompted it: the signed version (its id), or an addendum (its id). The document sent is the latest signed
    # version with every addendum up to and including the trigger.
    trigger_id: Mapped[str] = mapped_column(String(40))
    document_format: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # A short code (`http_503`, `timeout`, `connect_error`, `patient_erased`, ...); never a response body or PHI.
    last_error: Mapped[Optional[str]] = mapped_column(String(80))
    last_http_status: Mapped[Optional[int]] = mapped_column(Integer)
    # SHA-256 of the last document sent, so a receiver's copy can be matched to what was sent.
    document_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # The receiver's id for what it stored (FHIR `Location` header), when it gave one.
    remote_id: Mapped[Optional[str]] = mapped_column(String(300))
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
