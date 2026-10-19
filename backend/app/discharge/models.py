from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

ACTIVE_WORKFLOW_STATUSES = ("in_progress", "pending_approval")
_ACTIVE_SQL = text("status IN ('in_progress', 'pending_approval')")
# The patient has left (HL7 A03) but tasks are still open (ADR-002 item 11). Not active: the stay is over, so the
# patient can be readmitted and get a new workflow while this one's documentation is finished.
EXTERNALLY_DISCHARGED = "externally_discharged"


class Workflow(TenantMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        # At most one active discharge per patient, enforced by the database rather than a read-then-write check.
        Index(
            "uq_workflows_one_active_per_patient",
            "patient_id",
            unique=True,
            postgresql_where=_ACTIVE_SQL,
            sqlite_where=_ACTIVE_SQL,
        ),
        # ...and at most one per encounter (ADR-002 item 7).
        Index(
            "uq_workflows_one_active_per_encounter",
            "encounter_id",
            unique=True,
            postgresql_where=_ACTIVE_SQL,
            sqlite_where=_ACTIVE_SQL,
        ),
    )

    workflow_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wf"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    # The stay being discharged. Nullable for the transition only: the release before migration 0011 creates workflows
    # without it while the deploy rolls out; discharge.service links those to the patient's current encounter.
    encounter_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("encounters.encounter_id", ondelete="CASCADE"), index=True)
    initiated_by: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="in_progress", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    estimated_discharge_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # Time waiting for physician approval (operational metrics). `pending_approval_seconds` accumulates every stay in
    # pending_approval; NULL means the workflow predates tracking (migration 0006), not zero.
    pending_approval_since: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    pending_approval_seconds: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # Set when the discharge was done on paper during a downtime and entered afterwards: completed_at keeps the real
    # time, this is when it was entered (ADR-002 item 6).
    backfilled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # Set when the hospital's ADT system discharged the patient (HL7 A03) before this workflow was done (status
    # externally_discharged, ADR-002 item 11): when the patient actually left, and the policy deadline for the
    # tasks still open.
    departed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    documentation_due_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)


class Task(TenantMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_role_status", "assigned_role", "status"),)

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("task"))
    # Nullable: ad-hoc tasks can exist for a patient with no active discharge workflow.
    workflow_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(40), default="general")
    assigned_role: Mapped[str] = mapped_column(String(20))
    assigned_to: Mapped[Optional[str]] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1=high, 2=medium, 3=low
    created_by: Mapped[Optional[str]] = mapped_column(String(40))
    completed_by: Mapped[Optional[str]] = mapped_column(String(40))
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # Set when someone outside the assigned role changed the status (task:override): the latest reason, and by whom.
    override_reason: Mapped[Optional[str]] = mapped_column(Text)
    override_by: Mapped[Optional[str]] = mapped_column(String(40))
    # Set when the task was done on paper during a downtime and entered afterwards: completed_at is the time it was
    # done, this is when it was entered (ADR-002 item 6).
    backfilled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
