"""SCIM 2.0 Users endpoints for the hospital directory's provisioning connector (ADR-002 item 2; see identity/scim.py
for what is supported and what deactivation does)."""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit.service import write_audit
from app.core.db import DB
from app.core.security import CurrentUser, ensure_permission
from app.identity import integrations, scim
from app.identity.models import User

router = APIRouter(prefix="/scim/v2", tags=["SCIM provisioning"])


async def provisioning_client(
    db: DB,
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_key: Annotated[Optional[str], Header()] = None,
) -> CurrentUser:
    """An integration client holding `user:provision`. SCIM connectors send the key as a bearer token."""
    raw = authorization[7:] if authorization and authorization.startswith("Bearer ") else x_api_key
    client = await integrations.authenticate(db, raw)
    await ensure_permission(db, client, "user:provision")
    return client


Client = Annotated[CurrentUser, Depends(provisioning_client)]


def _respond(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=scim.MEDIA_TYPE)


def _error(e: scim.ScimError) -> JSONResponse:
    return _respond(e.body(), e.status)


def _base(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/api/scim/v2"


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise scim.ScimError(400, "The body is not JSON", "invalidSyntax")
    if not isinstance(body, dict):
        raise scim.ScimError(400, "The body must be a JSON object", "invalidSyntax")
    return body


async def _user(db, client: CurrentUser, user_id: str) -> User:
    user = await db.get(User, user_id)
    # The tenant check matters where RLS doesn't hide other tenants' rows (SQLite).
    if user is None or user.tenant_id != client.tenant_id:
        raise scim.ScimError(404, "User not found")
    return user


@router.get("/ServiceProviderConfig")
async def service_provider_config(client: Client):
    return _respond({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": scim.MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{"type": "oauthbearertoken", "name": "API key",
                                   "description": "The integration client's API key as a bearer token"}],
    })


@router.get("/Users")
async def list_users(
    request: Request, db: DB, client: Client,
    filter: Optional[str] = None,
    start_index: int = Query(1, alias="startIndex"),
    count: int = Query(100),
):
    try:
        parsed = scim.parse_filter(filter)
    except scim.ScimError as e:
        return _error(e)
    query = select(User).order_by(User.created_at, User.user_id)
    if parsed and parsed[0] == "userName":
        query = query.where(func.lower(User.email) == parsed[1].strip().lower())
    elif parsed:
        query = query.where(User.external_id == parsed[1])
    users = [u for u in await db.scalars(query) if u.tenant_id == client.tenant_id]
    start = max(start_index, 1)
    page = users[start - 1:start - 1 + max(0, min(count, scim.MAX_COUNT))]
    return _respond({"schemas": [scim.LIST_SCHEMA], "totalResults": len(users), "startIndex": start,
                     "itemsPerPage": len(page), "Resources": [scim.to_resource(u, _base(request)) for u in page]})


@router.post("/Users")
async def create_user(request: Request, db: DB, client: Client):
    try:
        body = await _json(request)
        user = scim.new_user(body)
        active = scim.as_bool(body["active"]) if "active" in body else True
    except scim.ScimError as e:
        return _error(e)
    if await db.scalar(select(User.user_id).where(func.lower(User.email) == user.email.lower())):
        return _error(scim.ScimError(409, "A user with this userName exists", "uniqueness"))
    try:
        # A savepoint: under RLS another hospital's account with this email is invisible, and only the unique
        # constraint finds it.
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:
        return _error(scim.ScimError(409, "A user with this userName exists", "uniqueness"))
    await write_audit(db, client, "SCIM_CREATE_USER", "user", user.user_id, "Pending approval", request)
    if not active:
        if action := await scim.set_active(db, user, False):
            await write_audit(db, client, action, "user", user.user_id, None, request)
    await db.flush()
    return _respond(scim.to_resource(user, _base(request)), 201)


@router.get("/Users/{user_id}")
async def get_user(user_id: str, request: Request, db: DB, client: Client):
    try:
        return _respond(scim.to_resource(await _user(db, client, user_id), _base(request)))
    except scim.ScimError as e:
        return _error(e)


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, db: DB, client: Client):
    try:
        user = await _user(db, client, user_id)
        changes = scim.patch_operations(await _json(request))
        active = None
        profile: dict[str, object] = {}
        for attribute, value in changes:
            if attribute == "active":
                active = scim.as_bool(value)
            elif attribute == "name" and isinstance(value, dict):
                profile["name"] = value
            elif attribute.startswith("name."):
                profile.setdefault("name", {})[attribute[5:]] = value  # type: ignore[index]
            else:
                profile[attribute] = value
    except scim.ScimError as e:
        return _error(e)

    if "externalId" in profile:
        external_id = profile.pop("externalId")
        user.external_id = str(external_id)[:200] if external_id else None
    name = scim.name_from(profile) if profile else None
    if name:
        user.name = name
    if changes:
        await write_audit(db, client, "SCIM_UPDATE_USER", "user", user.user_id,
                          "fields: " + ", ".join(sorted({a.split(".")[0] for a, _ in changes})), request)
    if active is not None:
        action = await scim.set_active(db, user, active)
        if action:
            await write_audit(db, client, action, "user", user.user_id, None, request)
    await db.flush()
    return _respond(scim.to_resource(user, _base(request)))
