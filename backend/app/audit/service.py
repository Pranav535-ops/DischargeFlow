"""Tamper-evident audit trail.

Each entry stores the hash of the previous entry in its tenant's chain, and its own hash covers every field plus
that link. Editing, deleting or reordering any entry breaks every hash after it, which `verify_chain` detects.
Database triggers additionally block UPDATE and DELETE on audit_logs outright.
"""
import hashlib
import json
from typing import Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import GENESIS_HASH, AuditChainHead, AuditLog
from app.core.db import utcnow
from app.core.ids import new_id
from app.core.security import CurrentUser

HASHED_FIELDS = (
    "tenant_id", "seq", "log_id", "user_id", "user_name", "action", "resource_type", "resource_id", "details",
    "ip_address", "timestamp", "prev_hash",
)


def entry_hash(entry: AuditLog) -> str:
    payload = {}
    for name in HASHED_FIELDS:
        value = getattr(entry, name)
        payload[name] = value.isoformat() if hasattr(value, "isoformat") else value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _locked_head(db: AsyncSession, tenant_id: str) -> AuditChainHead:
    head = await db.scalar(
        select(AuditChainHead).where(AuditChainHead.tenant_id == tenant_id).with_for_update()
    )
    if head is None:
        head = AuditChainHead(tenant_id=tenant_id, last_seq=0, last_hash=GENESIS_HASH)
        db.add(head)
        await db.flush()
    return head


async def write_audit(
    db: AsyncSession,
    user: CurrentUser,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    """Append an audit entry in the caller's transaction, so it commits or rolls back with the change it describes."""
    head = await _locked_head(db, user.tenant_id)
    entry = AuditLog(
        log_id=new_id("log"),  # set now: the id is part of the hashed content
        tenant_id=user.tenant_id,
        seq=head.last_seq + 1,
        user_id=user.user_id,
        user_name=user.name,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=request.client.host if request and request.client else None,
        timestamp=utcnow(),
        prev_hash=head.last_hash,
    )
    entry.hash = entry_hash(entry)
    db.add(entry)
    head.last_seq = entry.seq
    head.last_hash = entry.hash


async def verify_chain(db: AsyncSession, tenant_id: str) -> dict:
    """Recompute the tenant's chain. Returns the first broken sequence number, if any."""
    expected_prev, expected_seq, count = GENESIS_HASH, 1, 0
    rows = await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq))
    for entry in rows:
        count += 1
        if entry.seq != expected_seq or entry.prev_hash != expected_prev or entry.hash != entry_hash(entry):
            return {"valid": False, "entries": count, "broken_at_seq": entry.seq}
        expected_prev, expected_seq = entry.hash, entry.seq + 1
    head = await db.get(AuditChainHead, tenant_id)
    if head is not None and (head.last_seq != count or head.last_hash != expected_prev):
        # Trailing entries were removed: the head remembers a longer chain than the table holds.
        return {"valid": False, "entries": count, "broken_at_seq": count + 1}
    return {"valid": True, "entries": count, "broken_at_seq": None}
