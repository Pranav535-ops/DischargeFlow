from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin


class Escalation(TenantMixin, Base):
    """One escalation step of an overdue task (ADR-002 item 12). Level 1 tells the assignee, level 2 the supervising
    role. The (task, level) pair is unique, so concurrent or repeated runs escalate each step once.

    No PHI: ids, the task's category and priority, the patient's unit, and delivery state. The task title is free
    text and never copied here.
    """

    __tablename__ = "escalations"
    __table_args__ = (UniqueConstraint("task_id", "level", name="uq_escalations_task_level"),)

    escalation_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("esc"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(40))
    unit_id: Mapped[Optional[str]] = mapped_column(String(40))
    # Who was told: a named user (level 1 of an assigned task) or everyone in a role (on the unit, if unit-scoped).
    target_role: Mapped[str] = mapped_column(String(20))
    target_user_id: Mapped[Optional[str]] = mapped_column(String(40))
    # How many people the in-app notification reached (0: nobody holds the role on that unit, worth fixing).
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    # When the task crossed the step's threshold, and when this run escalated it.
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    escalated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # External page (secure messaging or paging gateway): not_required | pending | sent | failed | disabled.
    page_status: Mapped[str] = mapped_column(String(20), default="not_required")
    page_attempts: Mapped[int] = mapped_column(Integer, default=0)
    paged_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # Set by the next run after the task is completed.
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
