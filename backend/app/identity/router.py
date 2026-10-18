import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import DB
from app.core.security import (
    ACCESS_TOKEN_TTL,
    VALID_ROLES,
    AuthUser,
    CurrentUser,
    create_access_token,
    invalidate_identity,
    requires,
)
from app.core.tenancy import use_tenant
from app.identity import sessions
from app.identity.models import AuthSession, User
from app.notifications.service import notify_user

logger = logging.getLogger(__name__)

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
users_router = APIRouter(prefix="/users", tags=["Users"])


class RoleUpdate(BaseModel):
    role: str


async def fetch_oauth_profile(session_id: str) -> dict:
    """Exchange an Emergent OAuth session_id for the user's Google profile."""
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(EMERGENT_SESSION_URL, headers={"X-Session-ID": session_id}, timeout=10.0)
        except httpx.HTTPError as e:
            logger.error("Auth provider error: %s", e)
            raise HTTPException(status_code=401, detail="Authentication failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session_id")
    return resp.json()


def _token_response(response: Response, user: User, session: AuthSession, refresh_token: str) -> dict:
    sessions.set_refresh_cookie(response, refresh_token)
    return {
        "success": True,
        "token": create_access_token(user.user_id, session.session_id, user.tenant_id),
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "user": user.to_dict(),
    }


@auth_router.post("/session")
async def create_session(request: Request, response: Response, db: DB):
    """Exchange an OAuth session_id for an access token (body) and a refresh session (httpOnly cookie)."""
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    profile = await fetch_oauth_profile(session_id)
    # Single-hospital deployment: new users join the default tenant. SSO per hospital decides this later.
    await use_tenant(db, sessions.default_tenant())

    user = await db.scalar(select(User).where(User.email == profile["email"]))
    if user:
        user.name = profile["name"]
        user.picture = profile.get("picture")
    else:
        user = User(email=profile["email"], name=profile["name"], picture=profile.get("picture"), role="nurse")
        db.add(user)
        await db.flush()
        notify_user(db, user.user_id, "Welcome to DischargeFlow",
                    "Your account has been created. Contact an administrator to update your role.")
    await db.flush()

    session, refresh_token = await sessions.start_session(db, user, request)
    return _token_response(response, user, session, refresh_token)


class DevLogin(BaseModel):
    role: str
    name: str = ""


@auth_router.post("/dev-login", include_in_schema=False)
async def dev_login(body: DevLogin, request: Request, response: Response, db: DB):
    """Sign in as a test user of any role, for end-to-end tests. 404 unless ENABLE_DEV_LOGIN is set in a
    development/test environment; production settings force it off."""
    if not settings.enable_dev_login:
        raise HTTPException(status_code=404, detail="Not Found")
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    await use_tenant(db, sessions.default_tenant())
    email = f"e2e.{body.role}@dischargeflow.test"
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=body.name or f"E2E {body.role.capitalize()}", role=body.role)
        db.add(user)
        await db.flush()
    session, refresh_token = await sessions.start_session(db, user, request)
    return _token_response(response, user, session, refresh_token)


@auth_router.post("/refresh")
async def refresh(request: Request, response: Response, db: DB):
    """Rotate the refresh cookie and return a new access token."""
    result = await sessions.rotate(db, request.cookies.get(sessions.REFRESH_COOKIE))
    if result is None:
        # Returned rather than raised, so a revocation recorded during rotation still commits.
        refused = JSONResponse(status_code=401, content={"detail": "Session expired"})
        sessions.clear_refresh_cookie(refused)
        return refused
    return _token_response(response, result.user, result.session, result.token)


@auth_router.get("/me")
async def get_me(user: AuthUser):
    return user.model_dump(mode="json")


@auth_router.post("/logout")
async def logout(request: Request, response: Response, db: DB):
    await sessions.revoke_by_token(db, request.cookies.get(sessions.REFRESH_COOKIE))
    sessions.clear_refresh_cookie(response)
    return {"success": True}


@users_router.get("")
async def get_users(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    return [u.to_dict() for u in (await db.scalars(select(User).order_by(User.created_at)))]


@users_router.put("/{user_id}/role")
async def update_user_role(
    user_id: str, role_update: RoleUpdate, request: Request, db: DB,
    user: CurrentUser = Depends(requires("user:manage")),
):
    if role_update.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {list(VALID_ROLES)}")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    previous = target.role
    target.role = role_update.role
    invalidate_identity(user_id=user_id)
    await write_audit(db, user, "UPDATE_ROLE", "user", user_id, f"{previous} → {role_update.role}", request)
    return {"success": True, "role": role_update.role}
