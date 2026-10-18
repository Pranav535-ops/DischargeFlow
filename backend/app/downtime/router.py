from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import or_, select

from app.audit.service import audit_phi_read, write_audit
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, requires
from app.downtime import census as census_service
from app.downtime import service
from app.downtime.models import DowntimePeriod
from app.identity.models import Unit
from app.notifications.service import notify_role

router = APIRouter(prefix="/downtime", tags=["Downtime"])
census_router = APIRouter(prefix="/census", tags=["Downtime"])

STAFF_ROLES = ("physician", "nurse", "pharmacist", "admin")


class DowntimeStart(BaseModel):
    # Shown to every user in the banner, so operational only ("Database maintenance until 02:00"), never patient data.
    reason: str = Field(min_length=3, max_length=300)


class OutageRecord(BaseModel):
    started_at: AwareDatetime
    ended_at: AwareDatetime
    reason: str = Field(min_length=3, max_length=300)


async def _notify_staff(db, title: str, message: str) -> None:
    for role in STAFF_ROLES:
        await notify_role(db, role, title, message, "/census")


@router.get("/status")
async def downtime_status(db: DB, user: AuthUser):
    """Whether writes are refused right now. The frontend polls this to show the banner and disable controls."""
    return await service.read_only_status(db, user.tenant_id)


@router.post("/start")
async def start_downtime(body: DowntimeStart, request: Request, db: DB,
                         user: CurrentUser = Depends(requires("downtime:manage"))):
    """Put the tenant in read-only mode. Every write route answers 503 until an admin ends it."""
    if await service.open_period(db, user.tenant_id):
        raise HTTPException(status_code=409, detail="Read-only mode is already on")
    period = DowntimePeriod(kind="read_only", reason=body.reason.strip(), started_by=user.user_id)
    db.add(period)
    await db.flush()
    await _notify_staff(db, "Downtime: DischargeFlow is read-only",
                        f"{period.reason}. Print your unit's census and use the paper downtime forms.")
    await write_audit(db, user, "START_DOWNTIME", "downtime", period.downtime_id, "read-only mode on", request)
    return period.to_dict()


@router.post("/end")
async def end_downtime(request: Request, db: DB, user: CurrentUser = Depends(requires("downtime:manage"))):
    """End read-only mode (this route works while it is on). READ_ONLY_MODE, if set, still applies."""
    period = await service.open_period(db, user.tenant_id)
    if period is None:
        raise HTTPException(status_code=409, detail="Read-only mode is not on")
    period.ended_at = utcnow()
    period.ended_by = user.user_id
    await _notify_staff(db, "Downtime over: DischargeFlow is writable again",
                        "Enter the work done on paper with the time it was actually done (Backfill).")
    minutes = int((period.ended_at - period.started_at).total_seconds() // 60)
    await write_audit(db, user, "END_DOWNTIME", "downtime", period.downtime_id, f"read-only for {minutes} min",
                      request)
    return period.to_dict()


@router.post("/periods")
async def record_outage(body: OutageRecord, request: Request, db: DB,
                        user: CurrentUser = Depends(requires("downtime:manage"))):
    """Record an unplanned outage after the fact, so the work done on paper during it can be back-entered."""
    if body.ended_at <= body.started_at:
        raise HTTPException(status_code=400, detail="The outage must end after it starts")
    if body.ended_at > utcnow():
        raise HTTPException(status_code=400, detail="Record an outage once it is over")
    overlapping = await db.scalar(select(DowntimePeriod.downtime_id).where(
        DowntimePeriod.tenant_id == user.tenant_id, DowntimePeriod.started_at < body.ended_at,
        or_(DowntimePeriod.ended_at.is_(None), DowntimePeriod.ended_at > body.started_at),
    ))
    if overlapping:
        raise HTTPException(status_code=409, detail="This overlaps a downtime period that is already recorded")
    period = DowntimePeriod(kind="outage", reason=body.reason.strip(), started_at=body.started_at,
                            ended_at=body.ended_at, started_by=user.user_id, ended_by=user.user_id)
    db.add(period)
    await db.flush()
    await write_audit(db, user, "RECORD_OUTAGE", "downtime", period.downtime_id,
                      f"{body.started_at.isoformat()} to {body.ended_at.isoformat()}", request)
    return period.to_dict()


@router.get("/periods")
async def list_periods(db: DB, user: AuthUser):
    """Recent downtime periods, newest first: the windows work may be back-entered into."""
    stmt = select(DowntimePeriod).where(DowntimePeriod.tenant_id == user.tenant_id).order_by(DowntimePeriod.started_at.desc()).limit(50)
    return [p.to_dict() for p in await db.scalars(stmt)]


@census_router.get("")
async def discharge_census(request: Request, db: DB, user: AuthUser, unit_id: Optional[str] = None,
                           format: Literal["json", "csv"] = "json"):
    """Patients still in hospital, with discharge status and open tasks, for one unit (`unit_id`, or
    `unassigned`) or every unit the user can see. `format=csv` downloads it. One PHI_READ entry per request."""
    if unit_id and unit_id != census_service.UNASSIGNED and await db.get(Unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="Unit not found")
    census = await census_service.build_census(db, unit_id)
    scope = (census["unit"] or {}).get("code") or unit_id or "all"
    what = f"discharge census, {scope} ({census['totals']['patients']} records, {format})"
    if format == "csv":
        body = census_service.census_csv(census)
        filename = f"discharge-census-{scope}-{utcnow().strftime('%Y%m%d-%H%M')}.csv"
        await audit_phi_read(db, user, None, what, request)
        return Response(content=body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    # Audit last: the entry locks the tenant's audit chain head until the request commits.
    await audit_phi_read(db, user, None, what, request)
    return census
