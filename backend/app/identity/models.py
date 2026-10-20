from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin


class Tenant(Base):
    """A hospital. Every tenant-scoped row references one."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(TenantMixin, Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("user"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    picture: Mapped[Optional[str]] = mapped_column(String(2048))
    role: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # The directory's id for this person (SCIM externalId), set by provisioning (ADR-002 item 2).
    external_id: Mapped[Optional[str]] = mapped_column(String(200))
    # Set when the hospital directory deprovisions the account (SCIM active=false): it cannot sign in, and its
    # sessions were revoked. Cleared by reactivation. Distinct from role `pending`, which an admin sets.
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, onupdate=utcnow)


class Unit(TenantMixin, Base):
    """A ward or department (e.g. "4W"). Staff are members of units; patients are on one (ADR-002 item 3)."""

    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_units_tenant_code"),)

    unit_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("unit"))
    # Matches the point of care in HL7 PV1-3.1, so ADT admissions land on the right unit.
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserUnit(TenantMixin, Base):
    """Membership: with UNIT_SCOPED_ACCESS on, a user sees the patients on these units."""

    __tablename__ = "user_units"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("units.unit_id", ondelete="CASCADE"), primary_key=True,
                                         index=True)


class AuthSession(TenantMixin, Base):
    """One signed-in device. Holds the hash of its current refresh token, never the token itself."""

    __tablename__ = "auth_sessions"
    __api_exclude__ = frozenset({"current_hash", "previous_hash"})

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ses", 16))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    current_hash: Mapped[str] = mapped_column(String(64))
    # The token rotated out most recently, accepted briefly so concurrent tabs refreshing at once don't log out.
    previous_hash: Mapped[Optional[str]] = mapped_column(String(64))
    rotated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(40))
    user_agent: Mapped[Optional[str]] = mapped_column(String(300))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))


class RolePermission(Base):
    """Which role holds which permission. Shared across tenants; changed by migrations, read by the app."""

    __tablename__ = "role_permissions"

    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    permission: Mapped[str] = mapped_column(String(60), primary_key=True)


class IntegrationClient(TenantMixin, Base):
    """A hospital system that calls the API with an API key instead of a person signing in (the HL7 ADT feed).

    It acts as the non-human role `interface` (identity/integrations.py). Only the SHA-256 of the key's secret is
    stored; the key itself is shown once, when an admin creates the client.
    """

    __tablename__ = "integration_clients"
    __api_exclude__ = frozenset({"secret_hash"})

    client_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("intc", 16))
    name: Mapped[str] = mapped_column(String(200))
    secret_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)


class IdentityProvider(Base):
    """A hospital's OpenID Connect identity provider (ADR-002 item 2): usually a Cognito user pool federated with
    the hospital directory over SAML or OIDC. Its `issuer` decides which tenant a person signs in to.

    Shared across tenants like `tenants`: the login page lists providers before anyone has signed in, so the app
    role may only read it. Rows are added with `python -m app.cli sso-provider` (an operator, not a tenant admin).
    """

    __tablename__ = "identity_providers"
    __table_args__ = (UniqueConstraint("issuer", "client_id", name="uq_identity_providers_issuer_client"),)

    provider_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("idp", 12))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), index=True)
    # Shown on the sign-in button ("Sign in with St. Mary's").
    name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(200))
    authorize_url: Mapped[str] = mapped_column(String(500))
    token_url: Mapped[str] = mapped_column(String(500))
    # Default: the issuer's /.well-known/jwks.json (Cognito's layout).
    jwks_url: Mapped[Optional[str]] = mapped_column(String(500))
    scopes: Mapped[str] = mapped_column(String(200), default="openid email profile")
    # The ID-token claim listing the person's directory groups (a list, or a comma-separated string).
    groups_claim: Mapped[str] = mapped_column(String(100), default="groups")
    # Refuse ID tokens whose `amr` names no second factor.
    require_mfa: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class IdpGroupMapping(TenantMixin, Base):
    """A directory group's meaning in this hospital: a role, a unit membership, or both (ADR-002 item 2).

    When a provider has any mapping with a role, sign-in sets the person's role from their groups every time (no
    mapped group: `pending`); when it has any mapping with a unit, sign-in replaces their unit memberships likewise.
    `provider_id` is an `identity_providers` id, or `env` for the provider configured by OIDC_* variables.
    """

    __tablename__ = "idp_group_mappings"
    __table_args__ = (UniqueConstraint("tenant_id", "provider_id", "group_name", name="uq_idp_group_mappings_group"),)

    mapping_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("idpm", 12))
    provider_id: Mapped[str] = mapped_column(String(40), index=True)
    group_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Optional[str]] = mapped_column(String(20))
    unit_id: Mapped[Optional[str]] = mapped_column(ForeignKey("units.unit_id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
