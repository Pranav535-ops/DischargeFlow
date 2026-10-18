from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

JSONDoc = JSON().with_variant(JSONB(), "postgresql")
ACTION_STATUSES = ("pending", "confirmed", "cancelled", "expired", "failed")


class AssistantAction(TenantMixin, Base):
    """A write the assistant proposed. It runs only when the same user confirms it, at most once."""
    __tablename__ = "assistant_actions"

    action_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("act"))
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    tool: Mapped[str] = mapped_column(String(60))
    # Validated tool arguments with patient tokens already resolved to ids.
    arguments: Mapped[dict] = mapped_column(JSONDoc)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[Optional[dict]] = mapped_column(JSONDoc)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
