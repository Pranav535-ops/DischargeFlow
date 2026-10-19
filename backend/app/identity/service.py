"""Signing a person in, shared by every login method: the Google (Emergent) login and hospital OIDC in
identity/router.py, and the SMART on FHIR EHR launch in interop/smart.py (ADR-002 item 14).

Every method ends the same way: the account is matched by email (case-insensitive), an unknown account joins the
default tenant as `pending` if the registration rules allow it, and a refresh session plus a 15-minute access token
are issued.
"""
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core.config import settings
from app.core.security import ACCESS_TOKEN_TTL, CurrentUser, create_access_token
from app.core.tenancy import use_tenant
from app.identity import sessions
from app.identity.models import AuthSession, User, UserUnit
from app.identity.permissions import PENDING_ROLE
from app.notifications.service import notify_role


def registration_refusal(email: str) -> Optional[str]:
    """Why an account with no user row may not request access, or None if it may (as `pending`)."""
    if not settings.self_registration:
        return "This account has not been invited. Ask an administrator for access."
    domain = email.rsplit("@", 1)[-1].lower()
    if settings.allowed_email_domains and domain not in settings.allowed_email_domains:
        return "Sign-in is limited to the hospital's email domains."
    return None


def identity_of(user: User, session: AuthSession) -> CurrentUser:
    return CurrentUser(user_id=user.user_id, email=user.email, name=user.name, picture=user.picture, role=user.role,
                       tenant_id=user.tenant_id, session_id=session.session_id, created_at=user.created_at)


async def token_response(db: AsyncSession, response: Response, user: User, session: AuthSession,
                         refresh_token: str) -> dict:
    sessions.set_refresh_cookie(response, refresh_token)
    unit_ids = sorted(await db.scalars(select(UserUnit.unit_id).where(UserUnit.user_id == user.user_id)))
    return {
        "success": True,
        "token": create_access_token(user.user_id, session.session_id, user.tenant_id),
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "idle_lock_seconds": int(settings.idle_lock_minutes * 60),
        # unit_ids: the frontend defaults new patients to the user's first unit.
        "user": {**user.to_dict(), "unit_ids": unit_ids},
    }


async def find_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    return await db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


async def start_user_session(db: AsyncSession, request: Request, user: User,
                             registered: bool = False) -> tuple[AuthSession, str]:
    session, refresh_token = await sessions.start_session(db, user, request)
    if registered:
        await write_audit(db, identity_of(user, session), "REQUEST_ACCESS", "user", user.user_id, "Pending approval",
                          request)
    return session, refresh_token


async def match_or_register(db: AsyncSession, email: str, name: str,
                            picture: Optional[str]) -> tuple[User, bool]:
    """The user with this email (profile refreshed), or a new `pending` one; 403 if registration refuses it.
    Returns (user, newly registered). The caller has selected the tenant."""
    email = email.strip()
    user = await find_user_by_email(db, email)
    if user:
        user.name = name
        user.picture = picture
        await db.flush()
        return user, False
    refusal = registration_refusal(email)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    user = User(email=email, name=name, picture=picture, role=PENDING_ROLE)
    db.add(user)
    await db.flush()
    await notify_role(db, "admin", "Access Request",
                      f"{user.name} ({user.email}) signed in and is waiting for approval.", "/admin")
    return user, True


async def sign_in(db: AsyncSession, request: Request, response: Response, email: str, name: str,
                  picture: Optional[str]) -> dict:
    # Single-hospital deployment: new users join the default tenant. SSO per hospital decides this later.
    await use_tenant(db, sessions.default_tenant())
    user, registered = await match_or_register(db, email, name, picture)
    session, refresh_token = await start_user_session(db, request, user, registered)
    return await token_response(db, response, user, session, refresh_token)
