from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)

    notification_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("notif"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(20), default="info")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    link: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
