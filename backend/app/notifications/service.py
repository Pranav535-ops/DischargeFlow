from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import unit_scope_enabled
from app.identity.models import User, UserUnit
from app.notifications.models import Notification


async def notify_role(
    db: AsyncSession, role: str, title: str, message: str, link: Optional[str] = None, unit_id: Optional[str] = None,
    type_: str = "info",
) -> int:
    """Fan a notification out to every user holding `role`.

    `unit_id` is the unit of the patient the message is about. With UNIT_SCOPED_ACCESS on, only members of that unit
    are told, since the text may name a patient the others can't see. None (or an unassigned patient) tells everyone.
    """
    stmt = select(User.user_id).where(User.role == role)
    if unit_id and unit_scope_enabled():
        stmt = stmt.where(User.user_id.in_(select(UserUnit.user_id).where(UserUnit.unit_id == unit_id)))
    user_ids = list(await db.scalars(stmt))
    db.add_all(Notification(user_id=uid, title=title, message=message, link=link, type=type_) for uid in user_ids)
    return len(user_ids)


def notify_user(db: AsyncSession, user_id: str, title: str, message: str, type_: str = "info",
                link: Optional[str] = None) -> None:
    db.add(Notification(user_id=user_id, title=title, message=message, type=type_, link=link))
