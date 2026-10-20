from datetime import date
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from app.capacity import service
from app.core.db import DB
from app.core.security import CurrentUser, ensure_permission, get_current_user, get_signed_in_identity, requires
from app.identity.integrations import authenticate

router = APIRouter(tags=["Capacity"])

BED_PERMISSION = "bed:manage"


async def bed_actor(request: Request, db: DB, x_api_key: Annotated[Optional[str], Header()] = None) -> CurrentUser:
    """A signed-in user, or the EVS system as an integration client (X-API-Key), holding bed:manage."""
    actor = (await authenticate(db, x_api_key) if x_api_key
             else await get_current_user(await get_signed_in_identity(request, db)))
    await ensure_permission(db, actor, BED_PERMISSION)
    return actor


BedActor = Annotated[CurrentUser, Depends(bed_actor)]


class BedDirtyIn(BaseModel):
    unit_id: Optional[str] = None
    room_number: str = Field(min_length=1, max_length=40)


class BedStatusIn(BaseModel):
    status: Literal["cleaning", "available"]


@router.get("/capacity/board")
async def get_board(db: DB, day: Optional[date] = Query(None, alias="date"),
                    user: CurrentUser = Depends(requires("capacity:read"))):
    """Expected and actual discharges by unit and hour, discharge before noon, and beds being turned over.
    Counts and bed locations only; unit-scoped users see their units."""
    return await service.board(db, user, day)


@router.get("/beds")
async def list_beds(db: DB, actor: BedActor,
                    status: Optional[Literal["dirty", "cleaning", "available", "cancelled"]] = None):
    """Bed turnovers, oldest first: the open ones (dirty, cleaning) unless `status` is given."""
    return await service.list_beds(db, status)


@router.post("/beds")
async def report_dirty_bed(body: BedDirtyIn, request: Request, db: DB, actor: BedActor):
    return await service.report_dirty(db, actor, body.unit_id, body.room_number, request)


@router.put("/beds/{turnover_id}")
async def advance_bed(turnover_id: str, body: BedStatusIn, request: Request, db: DB, actor: BedActor):
    """dirty -> cleaning -> available (dirty -> available when the start wasn't entered)."""
    return await service.advance(db, actor, turnover_id, body.status, request)
