from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.audit.models import AuditLog
from app.core.db import DB
from app.core.security import CurrentUser, require_roles

router = APIRouter(prefix="/audit", tags=["Audit"])


@router.get("/logs")
async def get_audit_logs(
    db: DB,
    limit: int = 50,
    resource_type: Optional[str] = None,
    user_id: Optional[str] = None,
    user: CurrentUser = Depends(require_roles(["admin"])),
):
    stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(min(max(limit, 1), 500))
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    return [log.to_dict() for log in (await db.scalars(stmt))]


@router.get("/patients/{patient_id}")
async def get_patient_audit_trail(
    patient_id: str, db: DB, user: CurrentUser = Depends(require_roles(["admin", "physician"]))
):
    stmt = select(AuditLog).where(AuditLog.resource_id == patient_id).order_by(AuditLog.timestamp.desc()).limit(100)
    return [log.to_dict() for log in (await db.scalars(stmt))]
