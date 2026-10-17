from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

GENESIS_HASH = "0" * 64


class AuditLog(TenantMixin, Base):
    """Who did what, when. Append-only (enforced by triggers and grants) and hash-chained per tenant.

    No foreign keys on purpose: audit rows must outlive the records they describe.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (UniqueConstraint("tenant_id", "seq", name="uq_audit_logs_tenant_seq"),)

    log_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("log"))
    seq: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    user_name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(40), index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    details: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditChainHead(Base):
    """Latest link of a tenant's audit chain. Locked while appending so entries are strictly ordered."""

    __tablename__ = "audit_chain_heads"

    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), primary_key=True
    )
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), default=GENESIS_HASH)
