"""SCIM 2.0 provisioning, a subset of RFC 7643/7644 (ADR-002 item 2): the hospital directory (Entra ID, Okta, or
Cognito's directory sync) creates accounts and deactivates them when someone leaves.

Supported: `POST /Users`, `GET /Users/{id}`, `GET /Users` with `filter=userName eq "…"` (or `externalId eq "…"`) and
`startIndex`/`count` paging, `PATCH /Users/{id}` replacing `active` (and `displayName`, `name`, `externalId`), plus
`GET /ServiceProviderConfig`. Not supported: PUT, DELETE, Groups, bulk, sorting, other filters (400 / 501).

The caller is an integration client whose role holds `user:provision`, sending its API key as a bearer token (what
SCIM connectors send) or in X-API-Key. Created accounts are `pending`: their role comes from the directory-group
mappings at their first SSO sign-in, or from an admin. `active=false` sets `deactivated_at`, revokes every session of
the account and drops its cached identities in this process (other instances drop theirs within 30 s, and refresh
is refused at once), so access ends without waiting for an admin.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.security import invalidate_identity
from app.identity.models import AuthSession, User

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA_TYPE = "application/scim+json"
MAX_COUNT = 200


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: Optional[str] = None):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type

    def body(self) -> dict:
        body = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def to_resource(user: User, base_url: str) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "id": user.user_id,
        "externalId": user.external_id,
        "userName": user.email,
        "displayName": user.name,
        "name": {"formatted": user.name},
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": user.deactivated_at is None,
        "meta": {"resourceType": "User", "created": _iso(user.created_at),
                 "lastModified": _iso(user.updated_at or user.created_at),
                 "location": f"{base_url}/Users/{user.user_id}"},
    }


def name_from(body: dict) -> Optional[str]:
    raw = body.get("name")
    name: dict = raw if isinstance(raw, dict) else {}
    given_family = " ".join(str(p).strip() for p in (name.get("givenName"), name.get("familyName")) if p)
    value = str(body.get("displayName") or name.get("formatted") or given_family or "").strip()
    return value[:200] or None


def as_bool(value) -> bool:
    """SCIM clients send booleans, and some (Entra ID) send "True"/"False" strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, "active must be a boolean", "invalidValue")


def parse_filter(raw: Optional[str]) -> Optional[tuple[str, str]]:
    """`userName eq "x"` or `externalId eq "x"` → (attribute, value). Anything else is refused."""
    if raw is None or not raw.strip():
        return None
    parts = raw.strip().split(None, 2)
    if len(parts) != 3 or parts[1].lower() != "eq" or parts[0].lower() not in ("username", "externalid"):
        raise ScimError(400, "Only filters of the form userName eq \"…\" or externalId eq \"…\" are supported",
                        "invalidFilter")
    value = parts[2].strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ScimError(400, "The filter value must be a quoted string", "invalidFilter")
    return ("userName" if parts[0].lower() == "username" else "externalId"), value[1:-1].replace('\\"', '"')


async def set_active(db: AsyncSession, user: User, active: bool) -> Optional[str]:
    """Apply `active`; returns the audit action when it changed anything."""
    if active and user.deactivated_at is not None:
        user.deactivated_at = None
        return "SCIM_REACTIVATE_USER"
    if not active and user.deactivated_at is None:
        user.deactivated_at = utcnow()
        await revoke_all_sessions(db, user.user_id, "deprovisioned")
        return "SCIM_DEACTIVATE_USER"
    return None


async def revoke_all_sessions(db: AsyncSession, user_id: str, reason: str) -> int:
    now = utcnow()
    revoked = 0
    for session in await db.scalars(select(AuthSession).where(AuthSession.user_id == user_id,
                                                              AuthSession.revoked_at.is_(None))):
        session.revoked_at = now
        session.revoked_reason = reason
        revoked += 1
    invalidate_identity(user_id=user_id)
    return revoked


def patch_operations(body: dict) -> list[tuple[str, object]]:
    """The (attribute, value) pairs a PatchOp replaces. Both shapes clients use: `{"path": "active", "value": false}`
    and `{"value": {"active": false}}` (no path)."""
    if PATCH_SCHEMA not in (body.get("schemas") or []):
        raise ScimError(400, "Expected a PatchOp request", "invalidSyntax")
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "Operations must be a non-empty list", "invalidSyntax")
    changes: list[tuple[str, object]] = []
    for op in ops:
        if not isinstance(op, dict) or str(op.get("op", "")).lower() not in ("replace", "add"):
            raise ScimError(400, "Only replace and add operations are supported", "invalidSyntax")
        path, value = op.get("path"), op.get("value")
        if path:
            changes.append((str(path), value))
        elif isinstance(value, dict):
            changes.extend((str(k), v) for k, v in value.items())
        else:
            raise ScimError(400, "An operation without a path needs an object value", "invalidSyntax")
    for attribute, _ in changes:
        if attribute not in ("active", "displayName", "externalId", "name", "name.formatted", "name.givenName",
                             "name.familyName"):
            raise ScimError(400, f"Attribute not supported: {attribute[:60]}", "invalidPath")
    return changes


def _assert_user_name(value) -> str:
    if not isinstance(value, str) or "@" not in value or len(value) > 320 or any(c.isspace() for c in value):
        raise ScimError(400, "userName must be the person's email address", "invalidValue")
    return value.strip()


def new_user(body: dict) -> User:
    if USER_SCHEMA not in (body.get("schemas") or []):
        raise ScimError(400, "Expected a User resource", "invalidSyntax")
    email = _assert_user_name(body.get("userName"))
    external_id = body.get("externalId")
    if external_id is not None and (not isinstance(external_id, str) or len(external_id) > 200):
        raise ScimError(400, "externalId must be a string of at most 200 characters", "invalidValue")
    return User(email=email, name=name_from(body) or email.split("@")[0], role="pending", external_id=external_id)

