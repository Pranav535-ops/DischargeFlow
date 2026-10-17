from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.audit.models import AuditLog
from app.audit.service import verify_chain
from app.core.db import DB
from app.core.security import CurrentUser, requires

router = APIRouter(prefix="/audit", tags=["Audit"])


@router.get("/logs")
async def get_audit_logs(
    db: DB,
    limit: int = 50,
    resource_type: Optional[str] = None,
    user_id: Optional[str] = None,
    user: CurrentUser = Depends(requires("audit:read")),
):
    stmt = select(AuditLog).order_by(AuditLog.seq.desc()).limit(min(max(limit, 1), 500))
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    return [log.to_dict() for log in (await db.scalars(stmt))]


@router.get("/verify")
async def verify_audit_chain(db: DB, user: CurrentUser = Depends(requires("audit:read"))):
    """Recompute this tenant's audit hash chain and report the first tampered entry, if any."""
    return await verify_chain(db, user.tenant_id)


@router.get("/patients/{patient_id}")
async def get_patient_audit_trail(
    patient_id: str, db: DB, user: CurrentUser = Depends(requires("audit:read_patient"))
):
    stmt = select(AuditLog).where(AuditLog.resource_id == patient_id).order_by(AuditLog.seq.desc()).limit(100)
    return [log.to_dict() for log in (await db.scalars(stmt))]
