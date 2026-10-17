import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.core.db import DB
from app.core.security import TOKEN_TTL, VALID_ROLES, AuthUser, CurrentUser, create_session_token, require_roles
from app.identity.models import User
from app.notifications.service import notify_user

logger = logging.getLogger(__name__)

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
users_router = APIRouter(prefix="/users", tags=["Users"])


class RoleUpdate(BaseModel):
    role: str


async def fetch_oauth_profile(session_id: str) -> dict:
    """Exchange an Emergent OAuth session_id for the user's profile. Replaced by Cognito in Phase 2."""
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
    """Exchange session_id for session_token."""
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    profile = await fetch_oauth_profile(session_id)

    user = await db.scalar(select(User).where(User.email == profile["email"]))
    if user:
        user.name = profile["name"]
        user.picture = profile.get("picture")
    else:
        user = User(email=profile["email"], name=profile["name"], picture=profile.get("picture"), role="nurse")
        db.add(user)
        await db.flush()
        notify_user(
            db,
            user.user_id,
            "Welcome to DischargeFlow",
            "Your account has been created. Contact an administrator to update your role.",
        )
    await db.flush()

    user_doc = user.to_dict()
    token = create_session_token(user_doc)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=int(TOKEN_TTL.total_seconds()),
    )
    return {"success": True, "token": token, "user": user_doc}


@auth_router.get("/me")
async def get_me(user: AuthUser):
    return user.model_dump()


@auth_router.post("/logout")
async def logout(response: Response):
    # Tokens are stateless; logout clears the cookie and the client drops its copy.
    response.delete_cookie(key="session_token", path="/", secure=True, samesite="none")
    return {"success": True}


@users_router.get("")
async def get_users(db: DB, user: CurrentUser = Depends(require_roles(["admin"]))):
    return [u.to_dict() for u in (await db.scalars(select(User).order_by(User.created_at)))]


@users_router.put("/{user_id}/role")
async def update_user_role(
    user_id: str, role_update: RoleUpdate, db: DB, user: CurrentUser = Depends(require_roles(["admin"]))
):
    if role_update.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {list(VALID_ROLES)}")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.role = role_update.role
    return {"success": True, "role": role_update.role}
