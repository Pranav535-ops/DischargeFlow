from datetime import datetime
from typing import Optional

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("user"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    picture: Mapped[Optional[str]] = mapped_column(String(2048))
    role: Mapped[str] = mapped_column(String(20), default="nurse", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, onupdate=utcnow)
