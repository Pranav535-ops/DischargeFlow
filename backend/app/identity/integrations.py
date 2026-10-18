"""API keys for hospital systems (integration clients), such as the interface engine that sends HL7 ADT messages.

A key is `<tenant>.<client_id>.<secret>`, sent in the `X-API-Key` header. Like the refresh token, the tenant prefix
lets the request bind its transaction to the tenant (RLS) before reading the client row, and only the secret's
SHA-256 is stored. A client acts as the role `interface`, which is not a role people can be given
(`permissions.ROLES`), and holds only the permissions migrations grant it (`adt:ingest`). Audit entries name the
client id as the actor.
"""
import hashlib
import hmac
import secrets
from typing import Annotated, Optional

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import DB, utcnow
from app.core.observability import user_id_var
from app.core.security import CurrentUser, ensure_permission
from app.core.tenancy import use_tenant
from app.identity.models import IntegrationClient
from app.identity.permissions import INTEGRATION_ROLE

API_KEY_HEADER = "X-API-Key"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _parse(raw: Optional[str]) -> Optional[tuple[str, str, str]]:
    parts = (raw or "").strip().split(".")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


async def create_client(db: AsyncSession, name: str, created_by: str) -> tuple[IntegrationClient, str]:
    """A new client and its API key. The key is returned once and never stored."""
    secret = secrets.token_urlsafe(32)
    client = IntegrationClient(name=name, secret_hash=_hash(secret), created_by=created_by)
    db.add(client)
    await db.flush()
    return client, f"{client.tenant_id}.{client.client_id}.{secret}"


def identity_for(client: IntegrationClient) -> CurrentUser:
    return CurrentUser(user_id=client.client_id, email="", name=f"{client.name} (integration)", role=INTEGRATION_ROLE,
                       tenant_id=client.tenant_id, session_id=client.client_id, created_at=client.created_at)


async def authenticate(db: AsyncSession, raw_key: Optional[str]) -> CurrentUser:
    parsed = _parse(raw_key)
    if parsed is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    tenant_id, client_id, secret = parsed
    await use_tenant(db, tenant_id)
    client = await db.get(IntegrationClient, client_id)
    # The tenant check matters where RLS doesn't hide other tenants' rows (SQLite, the owner role).
    if (client is None or client.tenant_id != tenant_id or client.revoked_at is not None
            or not hmac.compare_digest(client.secret_hash, _hash(secret))):
        raise HTTPException(status_code=401, detail="Invalid API key")
    client.last_used_at = utcnow()
    user_id_var.set(client.client_id)
    return identity_for(client)


def requires_integration(permission: str):
    """Dependency: an integration client's API key whose role holds `permission`."""

    async def checker(db: DB, x_api_key: Annotated[Optional[str], Header()] = None) -> CurrentUser:
        client = await authenticate(db, x_api_key)
        await ensure_permission(db, client, permission)
        return client

    return checker
