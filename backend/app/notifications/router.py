from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import DB, get_session_factory
from app.core.security import AuthUser
from app.notifications.models import Notification
from app.notifications.stream import notification_events

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def get_notifications(db: DB, user: AuthUser):
    stmt = (
        select(Notification)
        .where(Notification.user_id == user.user_id)
        .order_by(Notification.created_at.desc())
        .limit(100)
    )
    return [n.to_dict() for n in (await db.scalars(stmt))]


@router.get("/stream")
async def stream_notifications(
    request: Request,
    user: AuthUser,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
):
    """Server-Sent Events: a `notification` event per new notification for the signed-in user.

    Authenticated with the usual Bearer header (the frontend reads it with fetch, not EventSource, so the token
    never goes into a URL). The stream ends after NOTIFICATION_STREAM_SECONDS and the client reconnects.
    """
    events = notification_events(user, session_factory, duration=settings.notification_stream_seconds,
                                 is_disconnected=request.is_disconnected)
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.put("/read-all")
async def mark_all_read(db: DB, user: AuthUser):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.user_id, Notification.read.is_(False))
        .values(read=True)
    )
    return {"success": True}


@router.put("/{notification_id}/read")
async def mark_notification_read(notification_id: str, db: DB, user: AuthUser):
    await db.execute(
        update(Notification)
        .where(Notification.notification_id == notification_id, Notification.user_id == user.user_id)
        .values(read=True)
    )
    return {"success": True}
