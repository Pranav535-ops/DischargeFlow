import json
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core import crypto
from app.core.config import settings
from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin
from app.risk import lace

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


# An IETF BCP 47 language tag: a 2-3 letter language, then up to three subtags ("en", "es-MX", "zh-Hant-TW").
LANGUAGE_TAG_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$"


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
    # IETF BCP 47 code of the language the patient prefers for written instructions (ADR-002 item 18), e.g. "es".
    # Not encrypted: a language is not an identifier on its own (it is not one of the HIPAA Safe Harbor identifiers).
    preferred_language: Mapped[Optional[str]] = mapped_column(String(35))
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
    # The discharge plan (ADR-002 item 8): where the patient is expected to go (discharge.models.DISPOSITIONS, which
    # picks the workflow template), and the last daily review of the plan.
    planned_disposition: Mapped[Optional[str]] = mapped_column(String(30))
    plan_reviewed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    plan_reviewed_by: Mapped[Optional[str]] = mapped_column(String(40))
    # Readmission risk inputs (ADR-002 item 20, app.risk). Admission type is HL7 table 0007 (PV1-4, or set on the
    # plan); patient class is PV1-2 (E = emergency, which counts as a prior ED visit). The Charlson comorbidity flags
    # are health information, so they are a JSON list encrypted with the patient's data key (AAD: encounter id +
    # field); NULL means not recorded. `ed_visits_recorded` is a count staff took from the hospital's records, used
    # instead of counting this system's encounters (which only include stays the ADT feed sends).
    admission_type: Mapped[Optional[str]] = mapped_column(String(2))
    patient_class: Mapped[Optional[str]] = mapped_column(String(2))
    _comorbidities: Mapped[Optional[str]] = mapped_column("comorbidities", Text)
    ed_visits_recorded: Mapped[Optional[int]] = mapped_column(Integer)
    # The LACE score as last computed: total, version, and the detail (points per component and the non-identifying
    # inputs behind them: day counts, the admission type code, the Charlson index, visit counts; never condition names).
    readmission_score: Mapped[Optional[int]] = mapped_column(Integer)
    readmission_score_version: Mapped[Optional[str]] = mapped_column(String(20))
    readmission_score_detail: Mapped[Optional[dict]] = mapped_column(JSON)
    readmission_scored_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    def _aad(self, field: str = "visit_number") -> str:
        return f"{self.encounter_id}:{field}"

    def _patient_key(self) -> bytes:
        if self.patient is None:
            raise ValueError("Set Encounter.patient first: its identifiers are encrypted with the patient's key")
        if self.patient.phi_dek is None:
            self.patient.phi_dek = crypto.new_wrapped_dek()
        if self.encounter_id is None:
            self.encounter_id = new_id("enc")  # the id is part of the ciphertext's associated data
        return self.patient.phi_dek

    @property
    def comorbidities(self) -> Optional[list[str]]:
        """The recorded Charlson condition keys (app.risk.lace.COMORBIDITIES); None when not recorded or erased."""
        raw = self._comorbidities
        if raw is None or not crypto.is_encrypted(raw) or self.patient.phi_dek is None:
            return None
        return json.loads(crypto.decrypt_field(self.patient.phi_dek, raw, self._aad("comorbidities")))

    @comorbidities.setter
    def comorbidities(self, value: Optional[list[str]]) -> None:
        if value is None:
            self._comorbidities = None
            return
        keys = json.dumps(sorted(set(value)))
        self._comorbidities = crypto.encrypt_field(self._patient_key(), keys, self._aad("comorbidities"))

    def length_of_stay(self, today: Optional[date] = None) -> tuple[Optional[int], str]:
        """Days for the LACE L component and what they are: `actual` once the stay has ended; while in, `expected`
        (to the expected discharge date) or `so_far` (to today: no expected date, or it has passed). Calendar days in
        hospital time, so a same-day stay is 0. None without an admission time."""
        if self.admitted_at is None:
            return None, "unknown"
        zone = ZoneInfo(settings.hospital_timezone)
        admitted = self.admitted_at.astimezone(zone).date()
        if self.status != "active" and self.discharged_at is not None:
            return max((self.discharged_at.astimezone(zone).date() - admitted).days, 0), "actual"
        today = today or utcnow().astimezone(zone).date()
        expected = self.expected_discharge_date
        if expected is not None and expected >= today:
            return max((expected - admitted).days, 0), "expected"
        return max((today - admitted).days, 0), "so_far"

    def rescore(self, ed_visits_counted: Optional[int] = None, today: Optional[date] = None) -> lace.LaceScore:
        """Recompute the LACE score from this stay's fields and store it (only when it changed). Counting prior ED
        visits in this system needs a query, which app.risk.service.recompute runs and passes in; without it the
        count from the last full computation is reused, so a discharge or a plan change rescores without one."""
        stored = self.readmission_score_detail or {}
        if ed_visits_counted is None:
            ed_visits_counted = stored.get("ed_visits_counted")
        recorded = self.ed_visits_recorded
        visits = recorded if recorded is not None else ed_visits_counted
        days, basis = self.length_of_stay(today)
        result = lace.score(lace.LaceInputs(length_of_stay_days=days, admission_type=self.admission_type,
                                            comorbidities=self.comorbidities, ed_visits=visits))
        detail = {
            "length_of_stay": {"points": result.length_of_stay, "days": days, "basis": basis},
            "acuity": {"points": result.acuity, "admission_type": self.admission_type},
            "comorbidity": {"points": result.comorbidity, "charlson_index": result.charlson_index},
            "ed_visits": {"points": result.ed_visits, "visits": visits,
                          "source": "recorded" if recorded is not None else "encounters"},
            "ed_visits_counted": ed_visits_counted,
            "missing": result.missing,
        }
        if (self.readmission_score, self.readmission_score_version, self.readmission_score_detail) != (
                result.score, result.version, detail):
            self.readmission_score = result.score
            self.readmission_score_version = result.version
            self.readmission_score_detail = detail
            self.readmission_scored_at = utcnow()
        return result

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
        self._visit_number = crypto.encrypt_field(self._patient_key(), value, self._aad())

    def close(self, discharged_at: datetime, disposition: Optional[str] = None) -> None:
        self.status = "discharged"
        self.discharged_at = discharged_at
        if disposition:
            self.disposition = disposition
        if self.readmission_score_detail is not None:
            self.rescore()  # the actual length of stay replaces the expected one

    def reopen(self) -> None:
        """HL7 A13: the discharge was cancelled and the stay is open again."""
        self.status, self.discharged_at, self.disposition = "active", None, None
        if self.readmission_score_detail is not None:
            self.rescore()

    def shred(self) -> None:
        """Erasure: the visit number links back to the hospital's account records, so overwrite it, and the
        comorbidity flags are health information. The score's numbers stay: they identify no one."""
        if self._visit_number is not None:
            self._visit_number = REDACTED
        if self._comorbidities is not None:
            self._comorbidities = REDACTED
