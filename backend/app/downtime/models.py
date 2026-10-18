from datetime import datetime
from typing import Optional

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

_OPEN_SQL = text("ended_at IS NULL")


class DowntimePeriod(TenantMixin, Base):
    """A stretch of time when staff worked on paper (ADR-002 item 6).

    `read_only` periods are started and ended by an admin, and the API refuses writes while one is open. `outage`
    periods are recorded afterwards for an unplanned outage, when nobody could switch read-only mode on. Either kind
    is the window that work done on paper may be back-entered into (backfill).
    """

    __tablename__ = "downtime_periods"
    __table_args__ = (
        # At most one open (read-only) period per tenant.
        Index("uq_downtime_periods_one_open", "tenant_id", unique=True, postgresql_where=_OPEN_SQL,
              sqlite_where=_OPEN_SQL),
    )

    downtime_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dt"))
    kind: Mapped[str] = mapped_column(String(20))  # "read_only" | "outage"
    # Operational note shown in the banner ("Database maintenance until 02:00"). Never patient details.
    reason: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_by: Mapped[str] = mapped_column(String(40))
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    ended_by: Mapped[Optional[str]] = mapped_column(String(40))
