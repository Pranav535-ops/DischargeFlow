from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

ACTIVE_WORKFLOW_STATUSES = ("in_progress", "pending_approval")
_ACTIVE_SQL = text("status IN ('in_progress', 'pending_approval')")
# The patient has left (HL7 A03) but tasks are still open (ADR-002 item 11). Not active: the stay is over, so the
# patient can be readmitted and get a new workflow while this one's documentation is finished.
EXTERNALLY_DISCHARGED = "externally_discharged"

# Task SLA targets in hours by priority (1 high, 2 medium, 3 low), for tasks without a due time from a template.
# Policy defaults, not measurements; a hospital would set its own.
TASK_SLA_HOURS = {1: 4, 2: 12, 3: 24}

# Planned discharge dispositions (ADR-002 item 8): where the patient is expected to go. Each may have a workflow
# template (item 9); default templates exist for the first six.
DISPOSITIONS = ("home", "home_services", "snf", "rehab", "hospice", "ama", "transfer", "expired")
# Why a discharge is late (item 10). Coded so they can be counted; a note is optional except for `other`.
DELAY_CODES = ("medical", "social", "placement", "insurance", "transport", "pharmacy", "documentation", "other")
DELAY_KINDS = ("expected_date_slip", "task_sla_breach")


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
    # The template its tasks came from (ADR-002 item 9); NULL for the built-in standard checklist.
    template_id: Mapped[Optional[str]] = mapped_column(String(40))
    template_version: Mapped[Optional[int]] = mapped_column(Integer)
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
    # From a workflow template (ADR-002 item 9): when it is due, as hours from the start of the expected discharge
    # date (kept so the due time moves with the date), and the tasks that must be completed before it can start.
    # A task without due_at is due TASK_SLA_HOURS after creation (see due_time).
    due_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    due_offset_hours: Mapped[Optional[int]] = mapped_column(Integer)
    depends_on: Mapped[Optional[list]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


def sla_hours(priority: Optional[int]) -> int:
    return TASK_SLA_HOURS.get(priority or 2, TASK_SLA_HOURS[2])


def due_time(due_at: Optional[datetime], created_at: datetime, priority: Optional[int]) -> datetime:
    """When a task is due: its template due time, else the SLA target for its priority after creation."""
    return due_at or created_at + timedelta(hours=sla_hours(priority))


class WorkflowTemplate(TenantMixin, Base):
    """The tasks a discharge to one disposition needs (ADR-002 item 9), one template per disposition per tenant.

    `tasks` is a list of {key, title, description, category, assigned_role, priority, due_offset_hours, depends_on}
    (validated by discharge.templates.TemplateTask). Editing bumps `version`; a workflow records the version it used.
    """

    __tablename__ = "workflow_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "disposition", name="uq_workflow_templates_tenant_disposition"),)

    template_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wft"))
    disposition: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    tasks: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class DelayReason(TenantMixin, Base):
    """Why a discharge is late (ADR-002 item 10): the expected date slipped, or a task breached its due time.

    The code is what gets counted. The optional note is free text that may name the patient, so it stays on this row
    (never in the audit entry) and erasure redacts it. Unit-scoped like other rows about a patient.
    """

    __tablename__ = "delay_reasons"
    __table_args__ = (Index("ix_delay_reasons_kind_created", "kind", "created_at"),)

    delay_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dly"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    encounter_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("encounters.encounter_id", ondelete="CASCADE"), index=True)
    task_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    code: Mapped[str] = mapped_column(String(30))
    note: Mapped[Optional[str]] = mapped_column(Text)
    # For a slip: how many days later the new expected date is (or how long ago the old one passed).
    slip_days: Mapped[Optional[int]] = mapped_column(Integer)
    recorded_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
