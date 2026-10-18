"""Access tokens, request identity and permission checks.

Access tokens are short-lived (15 min) HS256 JWTs carrying only the user, session and tenant ids. The user's role and
whether the session is still active are read from the database on each request (cached for 30 s per process), so a
role change or logout takes effect within that window instead of when a 7-day token expires.
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.core.db import DB, utcnow
from app.core.observability import user_id_var
from app.core.tenancy import use_tenant
from app.identity.models import AuthSession, User
from app.identity.permissions import ASSIGNABLE_ROLES, PENDING_ROLE, ROLES, permissions_for

ACCESS_TOKEN_TTL = timedelta(minutes=15)
VALID_ROLES = ROLES
# What an admin may set with PUT /api/users/{id}/role (or an invite): the working roles plus `pending` (suspend).
USER_ROLES = ASSIGNABLE_ROLES
_IDENTITY_CACHE_TTL = 30.0


class CurrentUser(BaseModel):
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    role: str
    tenant_id: str
    session_id: str
    created_at: datetime


def create_access_token(user_id: str, session_id: str, tenant_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "sid": session_id, "tid": tenant_id, "typ": "access", "iat": now,
               "exp": now + ACCESS_TOKEN_TTL}
    return jwt.encode(payload, settings.require_jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        claims = jwt.decode(token, settings.require_jwt_secret(), algorithms=["HS256"],
                            options={"require": ["sub", "sid", "tid", "exp"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if claims.get("typ") != "access":
        raise HTTPException(status_code=401, detail="Invalid token")
    return claims


_identity_cache: dict[str, tuple[float, CurrentUser]] = {}


def invalidate_identity(*, user_id: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """Drop cached identities in this process (other instances expire within the cache TTL)."""
    for sid, (_, cached) in list(_identity_cache.items()):
        if sid == session_id or cached.user_id == user_id:
            _identity_cache.pop(sid, None)


async def load_identity(db, user_id: str, session_id: str) -> Optional[CurrentUser]:
    row = (await db.execute(
        select(User, AuthSession)
        .join(AuthSession, AuthSession.user_id == User.user_id)
        .where(User.user_id == user_id, AuthSession.session_id == session_id)
    )).first()
    if row is None:
        return None
    user, session = row
    if session.revoked_at is not None or session.expires_at <= utcnow():
        return None
    return CurrentUser(user_id=user.user_id, email=user.email, name=user.name, picture=user.picture, role=user.role,
                       tenant_id=user.tenant_id, session_id=session.session_id, created_at=user.created_at)


async def get_signed_in_identity(request: Request, db: DB) -> CurrentUser:
    """The bearer token's identity, including accounts still waiting for approval. Only /api/auth/me uses this."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    claims = decode_access_token(auth_header[7:])

    # Bind the request's transaction to the token's tenant before reading any tenant-scoped row.
    await use_tenant(db, claims["tid"])

    user_id_var.set(claims["sub"])  # for log correlation only; authorization still waits for the checks below
    cached = _identity_cache.get(claims["sid"])
    if cached and cached[0] > time.monotonic() and cached[1].user_id == claims["sub"]:
        return cached[1]
    identity = await load_identity(db, claims["sub"], claims["sid"])
    if identity is None or identity.tenant_id != claims["tid"]:
        raise HTTPException(status_code=401, detail="Session is no longer active")
    _identity_cache[claims["sid"]] = (time.monotonic() + _IDENTITY_CACHE_TTL, identity)
    return identity


async def get_current_user(identity: Annotated[CurrentUser, Depends(get_signed_in_identity)]) -> CurrentUser:
    """The signed-in user, refused while their account waits for an admin's approval (role `pending`).

    Many reads need no permission (the patient list, for one), so a role with no permissions is not enough to keep a
    pending account away from patient data: it is refused here, before any endpoint runs.
    """
    if identity.role == PENDING_ROLE:
        raise HTTPException(status_code=403, detail="Account pending approval")
    return identity


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
SignedInIdentity = Annotated[CurrentUser, Depends(get_signed_in_identity)]


async def ensure_permission(db, user: CurrentUser, permission: Optional[str]) -> None:
    """The check behind requires(), for callers that are not endpoints (the assistant's tools)."""
    if permission and permission not in await permissions_for(db, user.role):
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def requires(permission: str):
    """Dependency: the signed-in user's role must hold `permission`."""

    async def checker(user: AuthUser, db: DB) -> CurrentUser:
        await ensure_permission(db, user, permission)
        return user

    return checker
