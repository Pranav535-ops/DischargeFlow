"""Refresh sessions.

The refresh token lives only in an httpOnly, SameSite=Strict cookie scoped to /api/auth, so page scripts can't read
it and other sites can't send it. Every refresh rotates it. Presenting the previous token is tolerated for a few
seconds (two tabs refreshing at once); after that it means the token leaked, and the whole session is revoked.

Each new secret is an HMAC of the one presented, so tabs that refresh the same token at once all get the same next
token, whichever response the browser stores last. Without that, a tab could keep a token that a concurrent refresh had
already rotated out, and its next refresh would look like reuse and log the user out.

Shared workstations (ADR-002 item 4): the browser locks itself after IDLE_LOCK_MINUTES without input and revokes the
session through /auth/logout. A tab that is closed or killed never gets to do that, so a session not refreshed for
SESSION_IDLE_MINUTES is revoked here as well. An open tab refreshes about every 14 minutes, which sets the floor.
"""
import base64
import hashlib
import hmac
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


def _next_secret(session_id: str, secret: str) -> str:
    """The secret that replaces `secret` when it is rotated. Only the server can compute it."""
    key = hmac.new(settings.require_jwt_secret().encode(), b"df-refresh-rotation", hashlib.sha256).digest()
    mac = hmac.new(key, f"{session_id}.{secret}".encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


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
    if now - session.last_used_at > timedelta(minutes=settings.session_idle_minutes):
        await revoke(db, session, "idle_timeout")
        return None

    presented = _hash(secret)
    new_secret = _next_secret(session_id, secret)
    in_grace = bool(session.rotated_at and now - session.rotated_at <= ROTATION_GRACE)
    if presented == session.current_hash:
        session.previous_hash = session.current_hash
        session.current_hash = _hash(new_secret)
        session.rotated_at = now
    elif presented == session.previous_hash and in_grace:
        # Concurrent refresh from another tab: it gets the token the first refresh issued, which is still current.
        if _hash(new_secret) != session.current_hash:
            # Rotated by a release that drew secrets at random, so that token can't be reissued; rotate once more.
            session.previous_hash = session.current_hash
            session.current_hash = _hash(new_secret)
            session.rotated_at = now
    else:
        # The previous token after the grace window, or the one before it, means a rotated-out token was replayed.
        older = presented == session.previous_hash or _hash(new_secret) == session.previous_hash
        if older and not in_grace:
            await revoke(db, session, "refresh_token_reuse")
        return None
    session.last_used_at = now
    user = await db.get(User, session.user_id)
    if user is None:
        return None
    if user.deactivated_at is not None:
        # Deprovisioned by the hospital directory (SCIM); its sessions were revoked then, this is a backstop.
        await revoke(db, session, "deprovisioned")
        return None
    return Refreshed(session=session, user=user, token=_token(tenant_id, session_id, new_secret))


async def revoke(db: AsyncSession, session: AuthSession, reason: str) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        session.revoked_reason = reason
    invalidate_identity(session_id=session.session_id)


async def revoke_by_token(db: AsyncSession, raw_token: Optional[str], reason: str = "logout") -> None:
    parsed = parse_token(raw_token)
    if parsed is None:
        return
    tenant_id, session_id, secret = parsed
    await use_tenant(db, tenant_id)
    session = await db.get(AuthSession, session_id)
    if session is not None and _hash(secret) in (session.current_hash, session.previous_hash):
        await revoke(db, session, reason)


def default_tenant() -> str:
    return settings.default_tenant_id
