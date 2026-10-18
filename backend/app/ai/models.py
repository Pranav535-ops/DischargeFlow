from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

JSONDoc = JSON().with_variant(JSONB(), "postgresql")


class AIGeneration(TenantMixin, Base):
    """Provenance for one LLM call (or one summary generation, including its retry).

    Stores what produced the output and what it cost, never the prompt or output text: those carry PHI.
    `input_hash` is the SHA-256 of the de-identified prompt, so identical inputs can be recognised.
    """
    __tablename__ = "ai_generations"

    generation_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("gen"))
    purpose: Mapped[str] = mapped_column(String(30), index=True)  # discharge_summary | assistant
    status: Mapped[str] = mapped_column(String(20))  # succeeded | invalid_output | failed
    user_id: Mapped[str] = mapped_column(String(40))
    workflow_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    patient_id: Mapped[Optional[str]] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(60), index=True)
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    grounding_warnings: Mapped[list] = mapped_column(JSONDoc, default=list)
    error: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DischargeSummary(TenantMixin, Base):
    __tablename__ = "discharge_summaries"

    summary_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sum"))
    # One summary per workflow, enforced by the unique constraint.
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), unique=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    # The current text: the AI draft until a physician approves it, then the approved (possibly edited) version.
    content: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(20), default="ai")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[Optional[str]] = mapped_column(String(40))
    approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Phase 3: structured draft, provenance, and what the approving physician changed.
    structured: Mapped[Optional[dict]] = mapped_column(JSONDoc)
    generation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("ai_generations.generation_id", ondelete="SET NULL"))
    original_content: Mapped[Optional[str]] = mapped_column(Text)
    edit_diff: Mapped[Optional[str]] = mapped_column(Text)
    # difflib similarity of approved text to the AI draft (1.0 = approved unedited); the main quality signal.
    edit_similarity: Mapped[Optional[float]] = mapped_column(Float)
