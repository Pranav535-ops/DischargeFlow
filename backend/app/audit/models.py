from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id


class AuditLog(Base):
    """Who did what, when. No foreign keys on purpose: audit rows must outlive the records they describe."""

    __tablename__ = "audit_logs"

    log_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("log"))
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    user_name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(40), index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    details: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
