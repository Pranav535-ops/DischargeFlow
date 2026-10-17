"""Session token handling and role-based access dependencies."""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import settings

TOKEN_TTL = timedelta(days=7)
VALID_ROLES = ("physician", "nurse", "pharmacist", "admin")


class CurrentUser(BaseModel):
    """Identity carried in the session token. Phase 2 moves role lookup to the database."""

    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    role: str = "nurse"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def create_session_token(user: dict) -> str:
    created_at = user["created_at"]
    payload = {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture"),
        "role": user["role"],
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "exp": datetime.now(timezone.utc) + TOKEN_TTL,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def get_current_user(request: Request) -> CurrentUser:
    """Read the session token from the cookie or an Authorization: Bearer header."""
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return CurrentUser(**claims)


def require_roles(allowed_roles: list[str]):
    async def role_checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return role_checker


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
