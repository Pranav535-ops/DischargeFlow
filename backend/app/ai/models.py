import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core import crypto
from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin
from app.patients.models import REDACTED

if TYPE_CHECKING:
    from app.patients.models import Patient

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


# Summary text carries names and clinical details, so it is encrypted with the owning patient's data key.
ENCRYPTED_SUMMARY_FIELDS = ("content", "original_content", "edit_diff", "structured")
_JSON_FIELDS = frozenset({"structured"})


def _summary_property(field: str) -> property:
    def get(self: "DischargeSummary") -> Any:
        return self._read_phi(field)

    def set_(self: "DischargeSummary", value: Any) -> None:
        self._write_phi(field, value)

    return property(get, set_)


class DischargeSummary(TenantMixin, Base):
    __tablename__ = "discharge_summaries"

    summary_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sum"))
    # One summary per workflow, enforced by the unique constraint.
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), unique=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    # Always loaded with the summary: its data key decrypts the text, and erasing the patient destroys that key.
    patient: Mapped["Patient"] = relationship(lazy="joined", innerjoin=True)
    # The text columns hold ciphertext ("enc:v1:...") behind public synonyms, as on Patient; `structured` holds the
    # ciphertext of its JSON. Rows written before migration 0005 hold plaintext until `app.cli migrate` backfills them.
    # The current text: the AI draft until a physician approves it, then the approved (possibly edited) version.
    _content: Mapped[str] = mapped_column("content", Text)
    generated_by: Mapped[str] = mapped_column(String(20), default="ai")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[Optional[str]] = mapped_column(String(40))
    approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Phase 3: structured draft, provenance, and what the approving physician changed.
    _structured: Mapped[Optional[str]] = mapped_column("structured", Text)
    generation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("ai_generations.generation_id", ondelete="SET NULL"))
    _original_content: Mapped[Optional[str]] = mapped_column("original_content", Text)
    _edit_diff: Mapped[Optional[str]] = mapped_column("edit_diff", Text)
    # difflib similarity of approved text to the AI draft (1.0 = approved unedited); the main quality signal.
    edit_similarity: Mapped[Optional[float]] = mapped_column(Float)

    content = synonym("_content", descriptor=_summary_property("content"))
    structured = synonym("_structured", descriptor=_summary_property("structured"))
    original_content = synonym("_original_content", descriptor=_summary_property("original_content"))
    edit_diff = synonym("_edit_diff", descriptor=_summary_property("edit_diff"))

    def _aad(self, field: str) -> str:
        return f"{self.summary_id}:{field}"

    def _read_phi(self, field: str) -> Any:
        raw = getattr(self, f"_{field}")
        if raw is None:
            return None
        if raw == REDACTED or (crypto.is_encrypted(raw) and self.patient.phi_dek is None):
            # Erased: overwritten, or ciphertext whose key was destroyed (unreadable by design).
            return None if field in _JSON_FIELDS else REDACTED
        if crypto.is_encrypted(raw):
            raw = crypto.decrypt_field(self.patient.phi_dek, raw, self._aad(field))
        # Otherwise legacy plaintext, readable until the backfill encrypts it.
        return json.loads(raw) if field in _JSON_FIELDS else raw

    def _write_phi(self, field: str, value: Any) -> None:
        if value is None:
            setattr(self, f"_{field}", None)
            return
        if self.patient is None:
            raise ValueError("Set DischargeSummary.patient before its text: the text is encrypted with its key")
        if self.patient.phi_dek is None:
            if self.patient.gdpr_erased:
                raise ValueError("Cannot store a summary for an erased patient")
            self.patient.phi_dek = crypto.new_wrapped_dek()  # legacy patient whose own fields await the backfill
        if self.summary_id is None:
            self.summary_id = new_id("sum")  # the id is part of the ciphertext's associated data
        plaintext = json.dumps(value) if field in _JSON_FIELDS else value
        setattr(self, f"_{field}", crypto.encrypt_field(self.patient.phi_dek, plaintext, self._aad(field)))

    @property
    def needs_encryption(self) -> bool:
        return any(
            raw is not None and raw != REDACTED and not crypto.is_encrypted(raw)
            for raw in (getattr(self, f"_{f}") for f in ENCRYPTED_SUMMARY_FIELDS)
        )

    def encrypt_legacy_fields(self) -> None:
        if self.patient.gdpr_erased:
            self.shred()  # plaintext left behind by an erasure that predates summary encryption
            return
        for field in ENCRYPTED_SUMMARY_FIELDS:
            value = self._read_phi(field)
            if value is not None:
                self._write_phi(field, value)

    def shred(self) -> None:
        """Erasure: overwrite the text. Its ciphertext is already unreadable once the patient's key is gone."""
        for field in ENCRYPTED_SUMMARY_FIELDS:
            if getattr(self, f"_{field}") is not None:
                setattr(self, f"_{field}", REDACTED)
