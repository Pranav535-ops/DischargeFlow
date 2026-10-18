from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

ACTIVE_WORKFLOW_STATUSES = ("in_progress", "pending_approval")
_ACTIVE_SQL = text("status IN ('in_progress', 'pending_approval')")


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
    )

    workflow_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wf"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    initiated_by: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="in_progress", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    estimated_discharge_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # Time waiting for physician approval (operational metrics). `pending_approval_seconds` accumulates every stay in
    # pending_approval; NULL means the workflow predates tracking (migration 0006), not zero.
    pending_approval_since: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    pending_approval_seconds: Mapped[Optional[int]] = mapped_column(Integer, default=0)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
