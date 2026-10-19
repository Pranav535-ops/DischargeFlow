from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin


class HL7Message(TenantMixin, Base):
    """One inbound HL7 message and the ACK it got. Never the message itself: it is full of PHI.

    The unique (client, MSH-10 control id) pair makes retries idempotent: a resent message gets its original ACK
    back instead of being applied twice.
    """

    __tablename__ = "hl7_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", "control_id", name="uq_hl7_messages_client_control"),
    )

    message_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("hl7", 16))
    client_id: Mapped[str] = mapped_column(
        ForeignKey("integration_clients.client_id", ondelete="RESTRICT"), index=True
    )
    control_id: Mapped[str] = mapped_column(String(199))
    message_type: Mapped[str] = mapped_column(String(20))  # e.g. "ADT^A01"
    ack_code: Mapped[str] = mapped_column(String(2))  # AA, AE or AR
    error_code: Mapped[Optional[str]] = mapped_column(String(4))  # HL7 table 0357
    ack_text: Mapped[Optional[str]] = mapped_column(String(500))  # the reason, never patient data
    patient_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("patients.patient_id", ondelete="SET NULL"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


PARKED_STATUSES = ("parked", "replayed", "expired", "discarded")


class HL7ParkedMessage(TenantMixin, Base):
    """An ADT message that arrived before its prerequisite (an A03 before the A01 that admits the MRN), kept until
    that arrives (ADR-002 item 11, finding 10).

    The message is full of PHI, so it is stored only as ciphertext under its own data key (wrapped by the KEK, as a
    patient's is), with the parked id as associated data. `mrn_index` is the blind index of the MRN it waits for. Once
    the message is replayed, expires or is discarded, the ciphertext and the key are both dropped.
    """

    __tablename__ = "hl7_parked_messages"
    __api_exclude__ = frozenset({"message", "dek", "mrn_index"})

    parked_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("hlp", 16))
    message_id: Mapped[str] = mapped_column(ForeignKey("hl7_messages.message_id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("integration_clients.client_id", ondelete="RESTRICT"), index=True
    )
    event: Mapped[str] = mapped_column(String(20))  # e.g. "ADT^A03"
    mrn_index: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[Optional[str]] = mapped_column(Text)  # ciphertext ("enc:v1:...") of the ER7 message
    dek: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), default="parked", index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    # The outcome of the replay (AA/AE/AR and the reason, never patient data).
    result_ack: Mapped[Optional[str]] = mapped_column(String(2))
    result_text: Mapped[Optional[str]] = mapped_column(String(500))


class SmartIssuer(TenantMixin, Base):
    """An EHR FHIR server allowed to launch DischargeFlow with SMART on FHIR (ADR-002 item 14): the per-tenant
    allow-list. A launch whose `iss` is not here, or is disabled, is refused before any request is made to it.

    `client_id` and `redirect_uri` are what the EHR registered for this app (a public client using PKCE; no secret).
    `mrn_system` / `mrn_authority` say which FHIR Patient.identifier is the MRN and which HL7 assigning authority the
    ADT feed sends for it, so the launch's patient maps to ours through the MRN blind index. `visit_system` is the
    Encounter.identifier system that carries the PV1-19 visit number.
    """

    __tablename__ = "smart_issuers"
    __table_args__ = (UniqueConstraint("tenant_id", "iss", name="uq_smart_issuers_tenant_iss"),)

    issuer_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("smi", 12))
    iss: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str] = mapped_column(String(200))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    mrn_system: Mapped[Optional[str]] = mapped_column(String(500))
    mrn_authority: Mapped[Optional[str]] = mapped_column(String(60))
    visit_system: Mapped[Optional[str]] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class EhrUserLink(TenantMixin, Base):
    """Which DischargeFlow user an EHR user (the id_token's `fhirUser`, e.g. Practitioner/123) is. Made by an admin,
    or on the first launch whose EHR identity carries an email that matches (the Google login's rule)."""

    __tablename__ = "ehr_user_links"
    __table_args__ = (UniqueConstraint("tenant_id", "issuer_id", "fhir_user", name="uq_ehr_user_links_fhir_user"),)

    link_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ehl", 12))
    issuer_id: Mapped[str] = mapped_column(ForeignKey("smart_issuers.issuer_id", ondelete="CASCADE"), index=True)
    fhir_user: Mapped[str] = mapped_column(String(500))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(40))  # None: linked by email at launch
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


SMART_LAUNCH_STATUSES = ("pending", "active", "failed", "ended")


class SmartLaunch(TenantMixin, Base):
    """One SMART EHR launch, from the redirect to the authorization server until the EHR token expires.

    `pending`: waiting for the authorization code (state and PKCE challenge stored as hashes / as sent). `active`:
    exchanged; the EHR access token is kept server-side only, as ciphertext under the launch's own data key (AAD:
    launch id + field), with the EHR's patient and encounter ids, and only the DischargeFlow session that made the
    launch may use it. When it expires, fails or the launch ends, the ciphertext and key are dropped. A refresh token
    is never requested or kept.
    """

    __tablename__ = "smart_launches"
    __api_exclude__ = frozenset({"state_hash", "code_challenge", "nonce", "dek", "access_token", "ehr_patient",
                                 "ehr_encounter", "authorize_endpoint", "token_endpoint", "jwks_uri", "oidc_issuer"})

    launch_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sml", 16))
    issuer_id: Mapped[str] = mapped_column(ForeignKey("smart_issuers.issuer_id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_challenge: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(64))
    # Discovered from {iss}/.well-known/smart-configuration when the launch started.
    authorize_endpoint: Mapped[str] = mapped_column(String(500))
    token_endpoint: Mapped[str] = mapped_column(String(500))
    jwks_uri: Mapped[Optional[str]] = mapped_column(String(500))
    oidc_issuer: Mapped[Optional[str]] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)  # of the pending launch, then of the EHR token
    # Set by the token exchange.
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("auth_sessions.session_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    fhir_user: Mapped[Optional[str]] = mapped_column(String(500))
    scope: Mapped[Optional[str]] = mapped_column(String(1000))
    dek: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    access_token: Mapped[Optional[str]] = mapped_column(Text)
    ehr_patient: Mapped[Optional[str]] = mapped_column(Text)
    ehr_encounter: Mapped[Optional[str]] = mapped_column(Text)
    patient_id: Mapped[Optional[str]] = mapped_column(ForeignKey("patients.patient_id", ondelete="SET NULL"),
                                                      index=True)
    encounter_id: Mapped[Optional[str]] = mapped_column(ForeignKey("encounters.encounter_id", ondelete="SET NULL"))
    # Why the patient context did not map (no MRN, unknown MRN, ...), never patient data.
    context_note: Mapped[Optional[str]] = mapped_column(String(200))
    failure: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
