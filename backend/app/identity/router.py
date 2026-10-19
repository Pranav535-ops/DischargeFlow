import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import DB, utcnow
from app.core.security import (
    USER_ROLES,
    VALID_ROLES,
    AuthUser,
    CurrentUser,
    SignedInIdentity,
    invalidate_identity,
    requires,
)
from app.core.tenancy import use_tenant
from app.identity import integrations, sessions, sso
from app.identity import service as identity_service
from app.identity.models import IntegrationClient, Unit, User, UserUnit

logger = logging.getLogger(__name__)

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
users_router = APIRouter(prefix="/users", tags=["Users"])
integrations_router = APIRouter(prefix="/integrations/clients", tags=["Integrations"])
units_router = APIRouter(prefix="/units", tags=["Units"])


class RoleUpdate(BaseModel):
    role: str


class Logout(BaseModel):
    # "idle" when the browser locked itself after IDLE_LOCK_MINUTES without input; kept as the revocation reason.
    reason: Literal["user", "idle"] = "user"


class UserInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    name: str = Field(default="", max_length=200)
    role: str


class UnitCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)


class UserUnitsUpdate(BaseModel):
    unit_ids: list[str] = Field(default_factory=list, max_length=100)


class IntegrationClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


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


@auth_router.post("/session")
async def create_session(request: Request, response: Response, db: DB):
    """Exchange an OAuth session_id for an access token (body) and a refresh session (httpOnly cookie).

    Known users (including ones an admin invited) sign in with the role on their row. An unknown account gets the
    no-access role `pending` if self-registration and the domain allow-list let it, and waits for an admin.
    """
    if not await sso.emergent_login_enabled(db):
        # Hospital SSO is configured (ADR-002 item 2): the demo Google login is off unless EMERGENT_LOGIN=true.
        raise HTTPException(status_code=404, detail="Not Found")
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    profile = await fetch_oauth_profile(session_id)
    return await identity_service.sign_in(db, request, response, profile["email"], profile["name"], profile.get("picture"))


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
    return await identity_service.token_response(db, response, user, session, refresh_token)


@auth_router.post("/refresh")
async def refresh(request: Request, response: Response, db: DB):
    """Rotate the refresh cookie and return a new access token."""
    result = await sessions.rotate(db, request.cookies.get(sessions.REFRESH_COOKIE))
    if result is None:
        # Returned rather than raised, so a revocation recorded during rotation still commits.
        refused = JSONResponse(status_code=401, content={"detail": "Session expired"})
        sessions.clear_refresh_cookie(refused)
        return refused
    return await identity_service.token_response(db, response, result.user, result.session, result.token)


@auth_router.get("/me")
async def get_me(user: SignedInIdentity):
    return user.model_dump(mode="json")


@auth_router.post("/logout")
async def logout(request: Request, response: Response, db: DB, body: Logout | None = None):
    reason = "idle_lock" if body and body.reason == "idle" else "logout"
    await sessions.revoke_by_token(db, request.cookies.get(sessions.REFRESH_COOKIE), reason)
    sessions.clear_refresh_cookie(response)
    return {"success": True}


@users_router.get("")
async def get_users(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    memberships: dict[str, list[str]] = {}
    for m in await db.scalars(select(UserUnit)):
        memberships.setdefault(m.user_id, []).append(m.unit_id)
    return [{**u.to_dict(), "unit_ids": sorted(memberships.get(u.user_id, []))}
            for u in (await db.scalars(select(User).order_by(User.created_at)))]


@users_router.post("")
async def invite_user(
    body: UserInvite, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))
):
    """Pre-create a user by email with a role. Their first Google sign-in with that email gets this role directly,
    even when self-registration is off or their domain is not on the allow-list."""
    if body.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {list(USER_ROLES)}")
    email = body.email.strip()
    if await db.scalar(select(User.user_id).where(func.lower(User.email) == email.lower())):
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    invited = User(email=email, name=body.name.strip() or email.split("@")[0], role=body.role)
    db.add(invited)
    await db.flush()
    await write_audit(db, user, "INVITE_USER", "user", invited.user_id, f"role={body.role}", request)
    return invited.to_dict()


@users_router.put("/{user_id}/role")
async def update_user_role(
    user_id: str, role_update: RoleUpdate, request: Request, db: DB,
    user: CurrentUser = Depends(requires("user:manage")),
):
    if role_update.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {list(USER_ROLES)}")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    previous = target.role
    target.role = role_update.role
    invalidate_identity(user_id=user_id)
    await write_audit(db, user, "UPDATE_ROLE", "user", user_id, f"{previous} → {role_update.role}", request)
    return {"success": True, "role": role_update.role}


@users_router.put("/{user_id}/units")
async def update_user_units(
    user_id: str, body: UserUnitsUpdate, request: Request, db: DB,
    user: CurrentUser = Depends(requires("user:manage")),
):
    """Replace the user's unit memberships. With UNIT_SCOPED_ACCESS on, these decide which patients they see."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    wanted = sorted(set(body.unit_ids))
    units = {u.unit_id: u for u in await db.scalars(select(Unit).where(Unit.unit_id.in_(wanted)))} if wanted else {}
    if len(units) != len(wanted):
        raise HTTPException(status_code=400, detail="Unknown unit")
    for membership in await db.scalars(select(UserUnit).where(UserUnit.user_id == user_id)):
        await db.delete(membership)
    await db.flush()
    db.add_all(UserUnit(user_id=user_id, unit_id=unit_id) for unit_id in wanted)
    invalidate_identity(user_id=user_id)
    codes = ", ".join(sorted(u.code for u in units.values())) or "none"
    await write_audit(db, user, "UPDATE_USER_UNITS", "user", user_id, f"units: {codes}"[:500], request)
    return {"success": True, "unit_ids": wanted}


# ---------- units ----------

@units_router.get("")
async def list_units(db: DB, user: AuthUser):
    return [u.to_dict() for u in await db.scalars(select(Unit).order_by(Unit.code))]


@units_router.post("")
async def create_unit(body: UnitCreate, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    """A ward or department. `code` should match the point of care the ADT feed sends in PV1-3.1."""
    code = body.code.strip().upper()
    if await db.scalar(select(Unit.unit_id).where(Unit.code == code)):
        raise HTTPException(status_code=409, detail="A unit with this code exists")
    unit = Unit(code=code, name=body.name.strip())
    db.add(unit)
    await db.flush()
    await write_audit(db, user, "CREATE_UNIT", "unit", unit.unit_id, code, request)
    return unit.to_dict()


# ---------- integration clients (API keys for hospital systems) ----------

@integrations_router.get("")
async def list_integration_clients(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    clients = await db.scalars(select(IntegrationClient).order_by(IntegrationClient.created_at))
    return [c.to_dict() for c in clients]


@integrations_router.post("")
async def create_integration_client(
    body: IntegrationClientCreate, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))
):
    """Create a client and return its API key. The key is shown only in this response."""
    client, api_key = await integrations.create_client(db, body.name, user.user_id)
    await write_audit(db, user, "CREATE_INTEGRATION_CLIENT", "integration_client", client.client_id, body.name, request)
    return {**client.to_dict(), "api_key": api_key}


@integrations_router.delete("/{client_id}")
async def revoke_integration_client(
    client_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))
):
    client = await db.get(IntegrationClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Integration client not found")
    if client.revoked_at is None:
        client.revoked_at = utcnow()
        await write_audit(db, user, "REVOKE_INTEGRATION_CLIENT", "integration_client", client_id, client.name, request)
    return client.to_dict()
