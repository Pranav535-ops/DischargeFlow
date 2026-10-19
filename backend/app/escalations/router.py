import hmac
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import DB, get_session_factory
from app.core.security import CurrentUser, ensure_permission, get_current_user, get_signed_in_identity, requires
from app.escalations import service
from app.identity.integrations import authenticate

router = APIRouter(prefix="/escalations", tags=["Escalations"])

RUN_PERMISSION = "escalation:run"
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _is_cron(request: Request) -> bool:
    """Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`. Off (never matches) until CRON_SECRET is set."""
    if not settings.cron_secret:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {settings.cron_secret}".encode())


@router.get("/run")
async def run_from_cron(request: Request, factory: SessionFactory):
    """Vercel Cron's call (cron jobs send GET). Runs every tenant, each in its own transaction."""
    if not _is_cron(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await service.run_all_tenants(factory)


@router.post("/run")
async def run_escalations(request: Request, db: DB, factory: SessionFactory,
                          x_api_key: Annotated[Optional[str], Header()] = None):
    """Escalate overdue tasks now. Idempotent: only steps that are due and not yet taken are taken.

    The cron secret runs every tenant; an integration client's API key, or a signed-in user, with `escalation:run`
    runs their own tenant.
    """
    if _is_cron(request):
        return await service.run_all_tenants(factory)
    actor: CurrentUser
    if x_api_key:
        actor = await authenticate(db, x_api_key)
    else:
        actor = await get_current_user(await get_signed_in_identity(request, db))
    await ensure_permission(db, actor, RUN_PERMISSION)
    return {"tenants": [await service.run_tenant(db, actor)]}


@router.get("")
async def list_escalations(db: DB, user: CurrentUser = Depends(requires("insights:read")),
                           limit: int = Query(200, ge=1, le=500)):
    """Active escalations (task still open), newest first, with the policy that produced them. No PHI."""
    return {
        "escalations": await service.active_escalations(db, user.tenant_id, limit),
        "policy": {
            service.PRIORITY_NAMES[p]: {
                "sla_hours": service.sla_hours(p),
                "steps": [{"level": s.level, "after_breach_hours": s.after_breach_hours, "page": s.page}
                          for s in steps],
            }
            for p, steps in service.POLICY.items()
        },
        "supervisor_roles": service.SUPERVISOR_ROLE,
        "pager": settings.escalation_pager,
        "scheduled": bool(settings.cron_secret),
    }
