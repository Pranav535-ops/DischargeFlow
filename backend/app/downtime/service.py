"""Downtime procedure (ADR-002 item 6): read-only mode and the window that paper records may be back-entered into.

Read-only mode is a per-tenant `downtime_periods` row that an admin opens and closes, so every serverless instance
sees it on its next write (an environment variable would need a redeploy and is per instance). `READ_ONLY_MODE=true`
forces it for every tenant, for when the database must not be written at all. While it is on, every write route
answers 503 (`enforce_read_only`, mounted on the whole /api router); reads, including their PHI-read audit entries,
keep working, and so do signing in and out and ending the downtime.
"""
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import DB, utcnow
from app.core.security import decode_access_token
from app.core.tenancy import use_tenant
from app.downtime.models import DowntimePeriod

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Writes that keep working in read-only mode: sessions (sign-in, refresh, logout, the SMART EHR launch) and ending
# the downtime.
EXEMPT_PREFIXES = ("/api/auth/",)
EXEMPT_PATHS = frozenset({"/api/downtime/end", "/api/smart/launch", "/api/smart/token"})
READ_ONLY_HEADER = "X-Read-Only"
READ_ONLY_DETAIL = ("DischargeFlow is in read-only mode for a downtime. Record work on the paper downtime forms; "
                    "it can be entered afterwards with the time it was done.")


async def open_period(db: AsyncSession, tenant_id: str) -> Optional[DowntimePeriod]:
    """The tenant's open read-only period, if any. Filtered by tenant explicitly: SQLite has no RLS."""
    return await db.scalar(select(DowntimePeriod).where(DowntimePeriod.tenant_id == tenant_id,
                                                        DowntimePeriod.ended_at.is_(None)))


async def read_only_status(db: AsyncSession, tenant_id: str) -> dict:
    period = await open_period(db, tenant_id)
    return {
        "read_only": settings.read_only_mode or period is not None,
        "forced": settings.read_only_mode,
        "period": period.to_dict() if period else None,
    }


def _tenant_of(request: Request) -> Optional[str]:
    """The tenant a write request is for: a valid access token's, or an integration key's prefix (the route itself
    authenticates the key). None when the request carries neither, and the route will refuse it anyway."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            return decode_access_token(auth[7:])["tid"]
        except HTTPException:
            return None
    key = request.headers.get("X-API-Key", "")
    tenant = key.split(".", 1)[0] if key.count(".") == 2 else ""
    return tenant or None


def _refuse() -> HTTPException:
    return HTTPException(status_code=503, detail=READ_ONLY_DETAIL, headers={READ_ONLY_HEADER: "true"})


async def enforce_read_only(request: Request, db: DB) -> None:
    """Router-wide dependency: refuse writes while the tenant (or the whole deployment) is read-only.

    It runs before the route's own dependencies, so nothing is written and no request body is validated. Reading the
    flag costs one indexed query per write request; reads skip it.
    """
    if request.method in SAFE_METHODS:
        return
    path = request.url.path
    if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return
    if settings.read_only_mode:
        raise _refuse()
    tenant_id = _tenant_of(request)
    if tenant_id is None:
        return
    await use_tenant(db, tenant_id)
    if await open_period(db, tenant_id) is not None:
        raise _refuse()


async def period_covering(db: AsyncSession, tenant_id: str, at: datetime) -> Optional[DowntimePeriod]:
    """The downtime period (read-only or recorded outage) that `at` falls in. Backfilled times must fall in one."""
    return await db.scalar(
        select(DowntimePeriod)
        .where(DowntimePeriod.tenant_id == tenant_id, DowntimePeriod.started_at <= at, or_(DowntimePeriod.ended_at.is_(None), DowntimePeriod.ended_at >= at))
        .order_by(DowntimePeriod.started_at.desc())
        .limit(1)
    )


async def backfill_period(db: AsyncSession, tenant_id: str, at: datetime, not_before: datetime) -> DowntimePeriod:
    """Validate the time paper work was actually done: in the past, after `not_before` (the record's creation) and
    inside a downtime period. Returns the period, which the audit entry names."""
    if at.tzinfo is None:
        raise HTTPException(status_code=422, detail="The time must include a timezone (e.g. 2026-10-19T14:30:00Z)")
    if at > utcnow():
        raise HTTPException(status_code=400, detail="The time it was done cannot be in the future")
    if at < not_before:
        raise HTTPException(status_code=400, detail="The time it was done is before the record existed")
    period = await period_covering(db, tenant_id, at)
    if period is None:
        raise HTTPException(status_code=400, detail="The time it was done is not inside a recorded downtime period. "
                                                    "An admin can record an unplanned outage first.")
    return period
