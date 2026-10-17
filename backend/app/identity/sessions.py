"""Refresh sessions.

The refresh token lives only in an httpOnly, SameSite=Strict cookie scoped to /api/auth, so page scripts can't read
it and other sites can't send it. Every refresh rotates it. Presenting the previous token is tolerated for a few
seconds (two tabs refreshing at once); after that it means the token leaked, and the whole session is revoked.
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import utcnow
from app.core.security import invalidate_identity
from app.core.tenancy import use_tenant
from app.identity.models import AuthSession, User

REFRESH_COOKIE = "df_refresh"
COOKIE_PATH = "/api/auth"
SESSION_TTL = timedelta(days=14)
ROTATION_GRACE = timedelta(seconds=30)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _token(tenant_id: str, session_id: str, secret: str) -> str:
    return f"{tenant_id}.{session_id}.{secret}"


def parse_token(token: Optional[str]) -> Optional[tuple[str, str, str]]:
    parts = (token or "").split(".")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=True,  # browsers accept Secure cookies on http://localhost, so this also works in development
        samesite="strict",
        path=COOKIE_PATH,
        max_age=int(SESSION_TTL.total_seconds()),
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path=COOKIE_PATH, secure=True, samesite="strict", httponly=True)


async def start_session(db: AsyncSession, user: User, request: Request) -> tuple[AuthSession, str]:
    secret = secrets.token_urlsafe(32)
    now = utcnow()
    session = AuthSession(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        current_hash=_hash(secret),
        created_at=now,
        last_used_at=now,
        expires_at=now + SESSION_TTL,
        user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        ip_address=request.client.host if request.client else None,
    )
    db.add(session)
    await db.flush()
    return session, _token(user.tenant_id, session.session_id, secret)


@dataclass
class Refreshed:
    session: AuthSession
    user: User
    token: str


async def rotate(db: AsyncSession, raw_token: Optional[str]) -> Optional[Refreshed]:
    """Validate and rotate a refresh token. Returns None (and may revoke the session) if it isn't acceptable."""
    parsed = parse_token(raw_token)
    if parsed is None:
        return None
    tenant_id, session_id, secret = parsed
    await use_tenant(db, tenant_id)
    session = await db.get(AuthSession, session_id)
    now = utcnow()
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        return None

    presented = _hash(secret)
    if presented == session.current_hash:
        pass
    elif presented == session.previous_hash and session.rotated_at and now - session.rotated_at <= ROTATION_GRACE:
        pass  # concurrent refresh from another tab; issue a fresh token without reusing the old one
    else:
        if presented == session.previous_hash:
            await revoke(db, session, "refresh_token_reuse")
        return None

    new_secret = secrets.token_urlsafe(32)
    session.previous_hash = session.current_hash
    session.current_hash = _hash(new_secret)
    session.rotated_at = now
    session.last_used_at = now
    user = await db.get(User, session.user_id)
    if user is None:
        return None
    return Refreshed(session=session, user=user, token=_token(tenant_id, session_id, new_secret))


async def revoke(db: AsyncSession, session: AuthSession, reason: str) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        session.revoked_reason = reason
    invalidate_identity(session_id=session.session_id)


async def revoke_by_token(db: AsyncSession, raw_token: Optional[str]) -> None:
    parsed = parse_token(raw_token)
    if parsed is None:
        return
    tenant_id, session_id, secret = parsed
    await use_tenant(db, tenant_id)
    session = await db.get(AuthSession, session_id)
    if session is not None and _hash(secret) in (session.current_hash, session.previous_hash):
        await revoke(db, session, "logout")


def default_tenant() -> str:
    return settings.default_tenant_id
