from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, onupdate=utcnow)


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
