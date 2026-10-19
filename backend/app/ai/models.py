import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


class SignedSummaryError(ValueError):
    """A signed summary's text is frozen: corrections go in an addendum (ADR-002 item 13)."""


def _phi_property(field: str) -> property:
    def get(self: "_PatientKeyedText") -> Any:
        return self._read_phi(field)

    def set_(self: "_PatientKeyedText", value: Any) -> None:
        self._write_phi(field, value)

    return property(get, set_)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _PatientKeyedText:
    """Text columns encrypted with the owning patient's data key, with the row id and field as associated data.

    Subclasses map `patient` (always joined), name their id attribute and prefix, and list their encrypted fields.
    Each field is a private `_<field>` column ("enc:v1:..." ciphertext) behind a public synonym.
    """
    _phi_fields: tuple[str, ...] = ()
    _json_fields: frozenset[str] = frozenset()
    _id_attr: str = ""
    _id_prefix: str = ""
    patient: "Patient"

    def _aad(self, field: str) -> str:
        return f"{getattr(self, self._id_attr)}:{field}"

    def _read_phi(self, field: str) -> Any:
        raw = getattr(self, f"_{field}")
        if raw is None:
            return None
        if raw == REDACTED or (crypto.is_encrypted(raw) and self.patient.phi_dek is None):
            # Erased: overwritten, or ciphertext whose key was destroyed (unreadable by design).
            return None if field in self._json_fields else REDACTED
        if crypto.is_encrypted(raw):
            raw = crypto.decrypt_field(self.patient.phi_dek, raw, self._aad(field))
        # Otherwise legacy plaintext, readable until the backfill encrypts it.
        return json.loads(raw) if field in self._json_fields else raw

    def _write_phi(self, field: str, value: Any) -> None:
        if value is None:
            setattr(self, f"_{field}", None)
            return
        if self.patient is None:
            raise ValueError(f"Set {type(self).__name__}.patient before its text: the text is encrypted with its key")
        if self.patient.phi_dek is None:
            if self.patient.gdpr_erased:
                raise ValueError("Cannot store a summary for an erased patient")
            self.patient.phi_dek = crypto.new_wrapped_dek()  # legacy patient whose own fields await the backfill
        if getattr(self, self._id_attr) is None:
            setattr(self, self._id_attr, new_id(self._id_prefix))  # the id is part of the associated data
        plaintext = json.dumps(value) if field in self._json_fields else value
        setattr(self, f"_{field}", crypto.encrypt_field(self.patient.phi_dek, plaintext, self._aad(field)))

    @property
    def needs_encryption(self) -> bool:
        return any(
            raw is not None and raw != REDACTED and not crypto.is_encrypted(raw)
            for raw in (getattr(self, f"_{f}") for f in self._phi_fields)
        )

    def encrypt_legacy_fields(self) -> None:
        if self.patient.gdpr_erased:
            self.shred()  # plaintext left behind by an erasure that predates summary encryption
            return
        for field in self._phi_fields:
            value = self._read_phi(field)
            if value is not None:
                self._write_phi(field, value)

    def shred(self) -> None:
        """Erasure: overwrite the text. Its ciphertext is already unreadable once the patient's key is gone."""
        for field in self._phi_fields:
            if getattr(self, f"_{field}") is not None:
                setattr(self, f"_{field}", REDACTED)


class DischargeSummary(TenantMixin, _PatientKeyedText, Base):
    __tablename__ = "discharge_summaries"
    _phi_fields = ENCRYPTED_SUMMARY_FIELDS
    _json_fields = frozenset({"structured"})
    _id_attr, _id_prefix = "summary_id", "sum"

    summary_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sum"))
    # One summary per workflow, enforced by the unique constraint.
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), unique=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    # Always loaded with the summary: its data key decrypts the text, and erasing the patient destroys that key.
    patient: Mapped["Patient"] = relationship(lazy="joined", innerjoin=True)
    # The text columns hold ciphertext ("enc:v1:...") behind public synonyms, as on Patient; `structured` holds the
    # ciphertext of its JSON. Rows written before migration 0005 hold plaintext until `app.cli migrate` backfills them.
    # The current text: the AI draft until a physician signs ("approves") it, then the signed (possibly edited)
    # version, frozen from then on. Signing also stores the text as DischargeSummaryVersion 1.
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

    # ADR-002 item 13: signed versions (oldest first) and the addenda added after signature, loaded with the summary.
    versions: Mapped[list["DischargeSummaryVersion"]] = relationship(
        lazy="selectin", order_by="DischargeSummaryVersion.version", cascade="all, delete-orphan",
        passive_deletes=True)
    addenda: Mapped[list["DischargeSummaryAddendum"]] = relationship(
        lazy="selectin", order_by="DischargeSummaryAddendum.signed_at", cascade="all, delete-orphan",
        passive_deletes=True)

    content = synonym("_content", descriptor=_phi_property("content"))
    structured = synonym("_structured", descriptor=_phi_property("structured"))
    original_content = synonym("_original_content", descriptor=_phi_property("original_content"))
    edit_diff = synonym("_edit_diff", descriptor=_phi_property("edit_diff"))

    def _write_phi(self, field: str, value: Any) -> None:
        # Re-encrypting the same text (the legacy backfill) is fine; changing signed text is not.
        if field == "content" and self.approved and value != self._read_phi("content"):
            raise SignedSummaryError("A signed discharge summary can't be edited; add an addendum instead")
        super()._write_phi(field, value)

    @property
    def latest_version(self) -> Optional["DischargeSummaryVersion"]:
        return self.versions[-1] if self.versions else None

    def record_signed_version(self, signed_by: str, signed_at: datetime) -> "DischargeSummaryVersion":
        """Freeze the current text as the next signed version (the caller has marked the summary approved)."""
        text = self.content
        version = DischargeSummaryVersion(
            # `patient` first: the text is encrypted with its data key as it is set.
            patient=self.patient, patient_id=self.patient_id, summary_id=self.summary_id,
            version=(self.latest_version.version + 1) if self.latest_version else 1,
            content=text, content_sha256=content_sha256(text), signed_by=signed_by, signed_at=signed_at,
        )
        self.versions.append(version)
        return version

    def addenda_for(self, version: Optional["DischargeSummaryVersion"]) -> list["DischargeSummaryAddendum"]:
        return [a for a in self.addenda if version is not None and a.version_id == version.version_id]

    def shred(self) -> None:
        super().shred()
        for row in [*self.versions, *self.addenda]:
            row.shred()


class DischargeSummaryVersion(TenantMixin, _PatientKeyedText, Base):
    """A signed, frozen version of a discharge summary (ADR-002 item 13).

    Signing creates version 1. The version number leaves room for a later correction workflow without a schema
    change, but nothing edits signed text today: what comes after signature is an addendum.
    """
    __tablename__ = "discharge_summary_versions"
    __table_args__ = (UniqueConstraint("summary_id", "version"),)
    _phi_fields = ("content",)
    _id_attr, _id_prefix = "version_id", "sver"

    version_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sver"))
    summary_id: Mapped[str] = mapped_column(
        ForeignKey("discharge_summaries.summary_id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    patient: Mapped["Patient"] = relationship(lazy="joined", innerjoin=True)
    version: Mapped[int] = mapped_column(Integer)
    _content: Mapped[str] = mapped_column("content", Text)
    # SHA-256 of the signed text, so a reader can check it is what was signed. Erasure clears it with the text.
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    signed_by: Mapped[str] = mapped_column(String(40))
    signed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    content = synonym("_content", descriptor=_phi_property("content"))

    def shred(self) -> None:
        super().shred()
        self.content_sha256 = None


class DischargeSummaryAddendum(TenantMixin, _PatientKeyedText, Base):
    """Text added to a signed summary, signed by its author as it is added. Never edited or removed."""
    __tablename__ = "discharge_summary_addenda"
    _phi_fields = ("content",)
    _id_attr, _id_prefix = "addendum_id", "add"

    addendum_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("add"))
    summary_id: Mapped[str] = mapped_column(
        ForeignKey("discharge_summaries.summary_id", ondelete="CASCADE"), index=True)
    # The signed version this addendum amends.
    version_id: Mapped[str] = mapped_column(
        ForeignKey("discharge_summary_versions.version_id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    patient: Mapped["Patient"] = relationship(lazy="joined", innerjoin=True)
    _content: Mapped[str] = mapped_column("content", Text)
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # The author, who signs the addendum by adding it.
    signed_by: Mapped[str] = mapped_column(String(40))
    signed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    content = synonym("_content", descriptor=_phi_property("content"))

    def shred(self) -> None:
        super().shred()
        self.content_sha256 = None
