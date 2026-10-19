from datetime import date, datetime, time, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Index, LargeBinary, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core import crypto
from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

PATIENT_STATUSES = ("admitted", "pending_discharge", "discharged")
REDACTED = "[REDACTED]"
# Identifiers encrypted at rest. They can't be sorted or searched in SQL; filter in Python after decrypting.
ENCRYPTED_FIELDS = ("name", "date_of_birth", "diagnosis")
# Encounter (one hospital stay) states: `active` until the patient leaves; at most one active per patient.
# `cancelled`: the admission was entered in error (HL7 A11).
ENCOUNTER_STATUSES = ("active", "discharged", "cancelled")
_ACTIVE_ENCOUNTER_SQL = text("status = 'active'")
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
    # admission_date, room_number and unit_id mirror the current encounter (ADR-002 item 7), which is the record of a
    # stay. They stay here because the patient API, unit-scope RLS and the previous release read them.
    admission_date: Mapped[str] = mapped_column(String(40), index=True)
    room_number: Mapped[str] = mapped_column(String(40))
    # The unit the patient is on. NULL means unassigned, which every user in the tenant can see (ADR-002 item 3).
    unit_id: Mapped[Optional[str]] = mapped_column(ForeignKey("units.unit_id", ondelete="SET NULL"), index=True)
    attending_physician: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="admitted", index=True)
    gdpr_consent: Mapped[Optional[bool]] = mapped_column(Boolean)
    gdpr_erased: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when an HL7 A40 merged this record into another (the surviving MRN): its stays moved there, its MRN index
    # was retired, and it is left out of the patient list. Kept for audit references.
    merged_into: Mapped[Optional[str]] = mapped_column(String(40), index=True)
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


class AccessGrant(TenantMixin, Base):
    """Break-glass: one user's time-limited access to one patient outside their units, with the reason they gave.

    The reason is kept here rather than in the audit entry (free text may name the patient, and audit rows can't be
    erased); erasure redacts it. Admins review grants at GET /api/access-grants.
    """

    __tablename__ = "access_grants"
    __table_args__ = (Index("ix_access_grants_patient_user", "patient_id", "user_id"),)

    grant_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("agr"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


def admitted_at_from(value: Optional[str]) -> Optional[datetime]:
    """The admission time an `admission_date` string stands for: an ISO datetime, or an ISO date (midnight UTC).
    None for anything else, such as NOT_PROVIDED from the ADT feed."""
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Encounter(TenantMixin, Base):
    """One hospital stay (HL7 PV1, FHIR Encounter): patients -> encounters -> workflows (ADR-002 item 7).

    A readmission is a new encounter, so earlier stays keep their dates and location. Unit scope follows the patient
    (rls.UNIT_SCOPED_TABLES). The visit number (PV1-19) identifies the patient's account at the hospital, so it is
    encrypted with the patient's data key like the other identifiers.
    """

    __tablename__ = "encounters"
    __table_args__ = (
        # At most one stay in progress per patient.
        Index("uq_encounters_one_active_per_patient", "patient_id", unique=True,
              postgresql_where=_ACTIVE_ENCOUNTER_SQL, sqlite_where=_ACTIVE_ENCOUNTER_SQL),
    )

    encounter_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("enc"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.patient_id", ondelete="CASCADE"), index=True)
    # Always loaded with the encounter: its data key decrypts the visit number.
    patient: Mapped["Patient"] = relationship(lazy="joined", innerjoin=True)
    _visit_number: Mapped[Optional[str]] = mapped_column("visit_number", Text)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    admitted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    discharged_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    unit_id: Mapped[Optional[str]] = mapped_column(ForeignKey("units.unit_id", ondelete="SET NULL"), index=True)
    room_number: Mapped[Optional[str]] = mapped_column(String(40))
    # Where the patient went (HL7 PV1-36 code, e.g. 01 home) and the planned discharge date (ADR-002 item 8).
    disposition: Mapped[Optional[str]] = mapped_column(String(40))
    expected_discharge_date: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    def _aad(self) -> str:
        return f"{self.encounter_id}:visit_number"

    @property
    def visit_number(self) -> Optional[str]:
        raw = self._visit_number
        if raw is None or not crypto.is_encrypted(raw):
            return raw  # none, or REDACTED after erasure
        if self.patient.phi_dek is None:
            return REDACTED
        return crypto.decrypt_field(self.patient.phi_dek, raw, self._aad())

    @visit_number.setter
    def visit_number(self, value: Optional[str]) -> None:
        value = (value or "").strip() or None
        if value is None:
            self._visit_number = None
            return
        if self.patient is None:
            raise ValueError("Set Encounter.patient before its visit number: it is encrypted with the patient's key")
        if self.patient.phi_dek is None:
            self.patient.phi_dek = crypto.new_wrapped_dek()
        if self.encounter_id is None:
            self.encounter_id = new_id("enc")  # the id is part of the ciphertext's associated data
        self._visit_number = crypto.encrypt_field(self.patient.phi_dek, value, self._aad())

    def close(self, discharged_at: datetime, disposition: Optional[str] = None) -> None:
        self.status = "discharged"
        self.discharged_at = discharged_at
        if disposition:
            self.disposition = disposition

    def shred(self) -> None:
        """Erasure: the visit number links back to the hospital's account records, so overwrite it."""
        if self._visit_number is not None:
            self._visit_number = REDACTED
