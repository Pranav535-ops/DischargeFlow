from typing import Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditLog
from app.core.security import CurrentUser


async def write_audit(
    db: AsyncSession,
    user: CurrentUser,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    """Record an audit event in the caller's transaction, so it commits or rolls back with the change it describes."""
    db.add(
        AuditLog(
            user_id=user.user_id,
            user_name=user.name,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=request.client.host if request and request.client else None,
        )
    )
