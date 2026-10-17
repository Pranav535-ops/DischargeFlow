from fastapi import APIRouter
from sqlalchemy import select, update

from app.core.db import DB
from app.core.security import AuthUser
from app.notifications.models import Notification

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
