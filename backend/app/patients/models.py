from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.core import crypto
from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

PATIENT_STATUSES = ("admitted", "pending_discharge", "discharged")
REDACTED = "[REDACTED]"
# Identifiers encrypted at rest. They can't be sorted or searched in SQL; filter in Python after decrypting.
ENCRYPTED_FIELDS = ("name", "date_of_birth", "diagnosis")
# Blind-index purpose for the MRN (crypto.blind_index). Changing it orphans every stored index.
MRN_INDEX_PURPOSE = "mrn"


def mrn_index_parts(tenant_id: str, authority: Optional[str], mrn: str) -> tuple[str, str, str]:
    """What the MRN blind index covers. The tenant is included so one MRN gives unrelated values in two hospitals,
    and the assigning authority (PID-3.4) because MRNs are only unique within the system that issued them."""
    return tenant_id, (authority or "").strip().upper(), mrn.strip()


def _phi_property(field: str) -> property:
    def get(self: "Patient") -> Optional[str]:
        return self._read_phi(field)

    def set_(self: "Patient", value: Optional[str]) -> None:
        self._write_phi(field, value)

    return property(get, set_)


class Patient(TenantMixin, Base):
    __tablename__ = "patients"
    __api_exclude__ = frozenset({"phi_dek", "mrn_index"})
    # One patient per MRN per tenant. NULLs (patients created in the UI, erased patients) don't collide.
    __table_args__ = (Index("uq_patients_tenant_mrn_index", "tenant_id", "mrn_index", unique=True),)

    patient_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pat"))
    # Ciphertext ("enc:v1:...") behind public synonyms that decrypt on read and encrypt on write.
    # Rows written before Phase 2 may still hold plaintext until `python -m app.cli encrypt-phi` runs.
    _name: Mapped[str] = mapped_column("name", Text)
    _date_of_birth: Mapped[str] = mapped_column("date_of_birth", Text)
    _diagnosis: Mapped[str] = mapped_column("diagnosis", Text)
    phi_dek: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    # Medical record number from the hospital's ADT feed (HL7 PID-3), encrypted like the other identifiers.
    # `mrn_index` is a keyed HMAC of it (see set_mrn), the only way to find a patient by MRN in SQL.
    _mrn: Mapped[Optional[str]] = mapped_column("mrn", Text)
    mrn_authority: Mapped[Optional[str]] = mapped_column(String(60))
    mrn_index: Mapped[Optional[str]] = mapped_column(String(64))
    admission_date: Mapped[str] = mapped_column(String(40), index=True)
    room_number: Mapped[str] = mapped_column(String(40))
    attending_physician: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="admitted", index=True)
    gdpr_consent: Mapped[Optional[bool]] = mapped_column(Boolean)
    gdpr_erased: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    name = synonym("_name", descriptor=_phi_property("name"))
    date_of_birth = synonym("_date_of_birth", descriptor=_phi_property("date_of_birth"))
    diagnosis = synonym("_diagnosis", descriptor=_phi_property("diagnosis"))
    mrn = synonym("_mrn", descriptor=_phi_property("mrn"))

    def _aad(self, field: str) -> str:
        return f"{self.patient_id}:{field}"

    def _read_phi(self, field: str) -> Optional[str]:
        raw = getattr(self, f"_{field}")
        if not crypto.is_encrypted(raw):
            return raw  # legacy plaintext, or the REDACTED marker after erasure
        if self.phi_dek is None:
            return REDACTED  # key destroyed: the ciphertext is unreadable by design
        return crypto.decrypt_field(self.phi_dek, raw, self._aad(field))

    def _write_phi(self, field: str, value: Optional[str]) -> None:
        if value is None:
            setattr(self, f"_{field}", None)
            return
        if self.patient_id is None:
            self.patient_id = new_id("pat")  # the id is part of the ciphertext's associated data
        if self.phi_dek is None:
            self.phi_dek = crypto.new_wrapped_dek()
        setattr(self, f"_{field}", crypto.encrypt_field(self.phi_dek, value, self._aad(field)))

    def set_mrn(self, tenant_id: str, authority: Optional[str], mrn: str) -> None:
        self.mrn = mrn.strip()
        self.mrn_authority = (authority or "").strip().upper() or None
        self.mrn_index = crypto.blind_index(MRN_INDEX_PURPOSE, *mrn_index_parts(tenant_id, authority, mrn))

    def reindex_mrn(self) -> bool:
        """Recompute the MRN index under the current KEK (after rotation). Returns whether it changed."""
        mrn = self.mrn
        if self.mrn_index is None or mrn is None or mrn == REDACTED:
            return False
        index = crypto.blind_index(MRN_INDEX_PURPOSE, *mrn_index_parts(self.tenant_id, self.mrn_authority, mrn))
        changed = index != self.mrn_index
        self.mrn_index = index
        return changed

    @property
    def needs_encryption(self) -> bool:
        return not self.gdpr_erased and any(
            getattr(self, f"_{f}") is not None and not crypto.is_encrypted(getattr(self, f"_{f}"))
            for f in ENCRYPTED_FIELDS
        )

    def encrypt_legacy_fields(self) -> None:
        for field in ENCRYPTED_FIELDS:
            value = self._read_phi(field)
            if value is not None:
                self._write_phi(field, value)

    def shred(self) -> None:
        """Right to erasure: drop the data key and overwrite identifiers. The row stays for audit references."""
        self.phi_dek = None
        for field in ENCRYPTED_FIELDS:
            setattr(self, f"_{field}", REDACTED)
        self.attending_physician = REDACTED
        # The index is a stable pseudonym that links back to the MRN in the hospital's own systems; drop it too.
        if self._mrn is not None:
            self._mrn = REDACTED
        self.mrn_index = None
        self.mrn_authority = None
        self.gdpr_erased = True
