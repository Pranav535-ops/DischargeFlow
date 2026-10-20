"""Hospital single sign-on routes (ADR-002 item 2): sign-in configuration for the login page, the OIDC sign-in
itself, and admins' directory-group mappings. The logic is in `identity/sso.py` and `identity/oidc.py`."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.audit.service import write_audit
from app.core.db import DB
from app.core.security import CurrentUser, requires
from app.identity import oidc, sso
from app.identity.models import IdpGroupMapping, Unit
from app.identity.permissions import ROLES

sso_auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
identity_providers_router = APIRouter(prefix="/identity-providers", tags=["Identity providers"])


class OidcSignIn(BaseModel):
    id_token: str = Field(min_length=1, max_length=16384)
    nonce: str = Field(min_length=16, max_length=256)
    provider_id: Optional[str] = Field(default=None, max_length=40)


class OidcCallback(BaseModel):
    provider_id: str = Field(min_length=1, max_length=40)
    code: str = Field(min_length=1, max_length=4096)
    # RFC 7636: 43 to 128 characters.
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=2048, pattern=r"^https?://")
    nonce: str = Field(min_length=16, max_length=256)


class GroupMappingCreate(BaseModel):
    group_name: str = Field(min_length=1, max_length=200)
    role: Optional[str] = None
    unit_id: Optional[str] = Field(default=None, max_length=40)


@sso_auth_router.get("/config")
async def auth_config(db: DB):
    """Which sign-in methods this deployment offers (public: the login page reads it before anyone signs in).

    `providers` are the hospitals' identity providers (public values only); `emergent` says whether the demo
    Google login is offered."""
    return {"providers": [p.public() for p in await sso.providers(db)],
            "emergent": await sso.emergent_login_enabled(db)}


@sso_auth_router.post("/oidc")
async def oidc_sign_in(body: OidcSignIn, request: Request, response: Response, db: DB):
    """Exchange a verified OIDC ID token for a session. The provider is `provider_id`, or the one whose issuer and
    client id the token names; it decides the tenant."""
    provider = (await sso.provider_by_id(db, body.provider_id) if body.provider_id
                else await sso.provider_for_token(db, body.id_token))
    claims = await oidc.verify_id_token(body.id_token, body.nonce, provider)
    return await sso.sign_in(db, request, response, provider, claims)


@sso_auth_router.post("/oidc/callback")
async def oidc_callback(body: OidcCallback, request: Request, response: Response, db: DB):
    """Finish the login page's authorization-code + PKCE redirect: redeem the code at the provider's token
    endpoint, verify the ID token it returns, and sign the person in."""
    provider = await sso.provider_by_id(db, body.provider_id)
    id_token = await oidc.exchange_code(provider, body.code, body.code_verifier, body.redirect_uri)
    claims = await oidc.verify_id_token(id_token, body.nonce, provider)
    return await sso.sign_in(db, request, response, provider, claims)


# ---------- admin: providers of this hospital and their group mappings ----------

@identity_providers_router.get("")
async def list_identity_providers(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    mappings: dict[str, list[dict]] = {}
    for m in await db.scalars(select(IdpGroupMapping).order_by(IdpGroupMapping.group_name)):
        mappings.setdefault(m.provider_id, []).append(m.to_dict())
    return [{**p.public(), "require_mfa": p.require_mfa, "groups_claim": p.groups_claim,
             "mappings": mappings.get(p.provider_id, [])}
            for p in await sso.providers(db, user.tenant_id)]


@identity_providers_router.post("/{provider_id}/mappings")
async def create_group_mapping(
    provider_id: str, body: GroupMappingCreate, request: Request, db: DB,
    user: CurrentUser = Depends(requires("user:manage")),
):
    """Map a directory group to a role, a unit, or both. Mapping any group to a role makes roles follow groups at
    every sign-in through this provider (unmapped people become `pending`); mapping a unit does the same for unit
    memberships."""
    provider = await sso.provider_by_id(db, provider_id)
    if provider.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Unknown identity provider")
    if body.role is None and body.unit_id is None:
        raise HTTPException(status_code=400, detail="Map the group to a role, a unit, or both")
    if body.role is not None and body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {list(ROLES)}")
    if body.unit_id is not None and await db.get(Unit, body.unit_id) is None:
        raise HTTPException(status_code=400, detail="Unknown unit")
    group = body.group_name.strip()
    if await db.scalar(select(IdpGroupMapping.mapping_id).where(
            IdpGroupMapping.provider_id == provider_id, IdpGroupMapping.group_name == group)):
        raise HTTPException(status_code=409, detail="This group is already mapped")
    mapping = IdpGroupMapping(provider_id=provider_id, group_name=group, role=body.role, unit_id=body.unit_id)
    db.add(mapping)
    await db.flush()
    await write_audit(db, user, "CREATE_IDP_GROUP_MAPPING", "idp_group_mapping", mapping.mapping_id,
                      f"provider={provider_id} role={body.role or '-'} unit={body.unit_id or '-'}", request)
    return mapping.to_dict()


@identity_providers_router.delete("/{provider_id}/mappings/{mapping_id}")
async def delete_group_mapping(
    provider_id: str, mapping_id: str, request: Request, db: DB,
    user: CurrentUser = Depends(requires("user:manage")),
):
    """Remove a mapping. It takes effect at each person's next sign-in, like a change in the directory."""
    mapping = await db.get(IdpGroupMapping, mapping_id)
    if mapping is None or mapping.provider_id != provider_id:
        raise HTTPException(status_code=404, detail="Mapping not found")
    await db.delete(mapping)
    await write_audit(db, user, "DELETE_IDP_GROUP_MAPPING", "idp_group_mapping", mapping_id,
                      f"provider={provider_id}", request)
    return {"success": True}
