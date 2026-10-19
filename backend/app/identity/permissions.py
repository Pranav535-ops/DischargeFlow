"""Role → permission mapping, stored in role_permissions and cached briefly per process.

Endpoints declare the permission they need (`requires("workflow:manage")`) instead of listing roles. Reads that
every signed-in user may perform need no permission.
"""
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import RolePermission

ROLES = ("physician", "nurse", "pharmacist", "admin")
# The role integration clients (hospital systems with an API key) act as. Not in ROLES: no person can be given it.
INTEGRATION_ROLE = "interface"
# New sign-ins that no admin has approved or invited yet. Holds no permissions, and `get_current_user` refuses it on
# every endpoint except /api/auth/me, so it cannot read patient data either. Admins can also set it to suspend someone.
PENDING_ROLE = "pending"
ASSIGNABLE_ROLES = ROLES + (PENDING_ROLE,)

# Source of truth for migrations' seed data. Admin implicitly holds every permission.
DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "patient:write": ("physician", "nurse"),
    "patient:erase": (),
    "workflow:manage": ("physician",),
    "summary:manage": ("physician",),
    "task:create": ("physician", "nurse"),
    # Change the status of a task assigned to another user or role (a reason is required and stored).
    "task:override": ("physician",),
    # Assign a task to a named user other than yourself.
    "task:assign": ("physician",),
    "user:manage": (),
    "audit:read": (),
    "audit:read_patient": ("physician",),
    "insights:read": (),
    "adt:ingest": (INTEGRATION_ROLE,),
    "claim:manage": ("nurse",),
    # Not limited to one's units when UNIT_SCOPED_ACCESS is on (ADR-002 item 3). The ADT feed matches any patient.
    "patient:read_all": (INTEGRATION_ROLE,),
    # Switch read-only (downtime) mode on and off, and record unplanned outages (ADR-002 item 6). Admin only.
    "downtime:manage": (),
}
ALL_PERMISSIONS = tuple(DEFAULT_ROLE_PERMISSIONS)


def default_rows() -> list[tuple[str, str]]:
    rows = [(role, perm) for perm, roles in DEFAULT_ROLE_PERMISSIONS.items() for role in roles]
    rows += [("admin", perm) for perm in ALL_PERMISSIONS]
    return sorted(rows)


_CACHE_TTL = 60.0
_cache: tuple[float, dict[str, frozenset[str]]] | None = None


async def permissions_for(db: AsyncSession, role: str) -> frozenset[str]:
    global _cache
    now = time.monotonic()
    if _cache is None or _cache[0] < now:
        mapping: dict[str, set[str]] = {}
        for row in await db.scalars(select(RolePermission)):
            mapping.setdefault(row.role, set()).add(row.permission)
        _cache = (now + _CACHE_TTL, {r: frozenset(p) for r, p in mapping.items()})
    return _cache[1].get(role, frozenset())


def clear_cache() -> None:
    global _cache
    _cache = None


async def seed_role_permissions(db: AsyncSession) -> None:
    """Insert the default mapping (used by tests and fresh dev databases built without migrations)."""
    existing = {(r.role, r.permission) for r in await db.scalars(select(RolePermission))}
    db.add_all(RolePermission(role=r, permission=p) for r, p in default_rows() if (r, p) not in existing)
    await db.flush()
    clear_cache()
