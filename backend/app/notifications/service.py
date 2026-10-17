from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import User
from app.notifications.models import Notification


async def notify_role(
    db: AsyncSession, role: str, title: str, message: str, link: Optional[str] = None
) -> None:
    """Fan a notification out to every user holding `role`."""
    user_ids = await db.scalars(select(User.user_id).where(User.role == role))
    db.add_all(Notification(user_id=uid, title=title, message=message, link=link) for uid in user_ids)


def notify_user(db: AsyncSession, user_id: str, title: str, message: str, type_: str = "info") -> None:
    db.add(Notification(user_id=user_id, title=title, message=message, type=type_))
