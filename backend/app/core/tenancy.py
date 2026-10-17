"""Tenant isolation.

Every tenant-scoped table carries `tenant_id`. On PostgreSQL, each request transaction switches to the
`dischargeflow_app` role (which does not bypass row-level security) and sets `app.tenant_id`; RLS policies then
hide other tenants' rows and reject writes into them. The table owner (migrations, CLI) is not subject to RLS.
SQLite has no RLS, so local SQLite development is effectively single-tenant.
"""
from contextvars import ContextVar

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

APP_ROLE = "dischargeflow_app"

current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)

# Cached per process: whether the RLS role exists in the connected Postgres database.
_app_role_available: bool | None = None


def _tenant_default() -> str:
    tenant = current_tenant_id.get()
    if tenant is None:
        raise RuntimeError("No tenant in context; call use_tenant() before writing tenant-scoped rows")
    return tenant


class TenantMixin:
    """Adds a non-null tenant_id, defaulting to the tenant of the current request."""

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(
            String(40), ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), index=True, default=_tenant_default
        )


async def _role_available(db: AsyncSession) -> bool:
    global _app_role_available
    if _app_role_available is None:
        _app_role_available = bool(
            await db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": APP_ROLE})
        )
    return _app_role_available


def reset_role_cache() -> None:
    global _app_role_available
    _app_role_available = None


async def use_tenant(db: AsyncSession, tenant_id: str) -> None:
    """Bind the current transaction (and new rows) to `tenant_id`."""
    current_tenant_id.set(tenant_id)
    if db.bind.dialect.name != "postgresql":
        return
    if await _role_available(db):
        await db.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
