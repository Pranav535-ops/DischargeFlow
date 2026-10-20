"""Hospital SSO sign-in per tenant (ADR-002 item 2): which identity provider a token belongs to, which tenant that
is, and what the person's directory groups mean there (role and unit memberships).

Rules, applied on every SSO sign-in after the token is verified (`oidc.verify_id_token`):
- The provider decides the tenant. Nothing in the token can choose another one.
- A deactivated account (SCIM `active=false`) is refused.
- If the provider has group mappings with a role, the role follows the person's groups every time: the most
  privileged mapped role, or `pending` when none of their groups maps to a role (removing someone from the directory
  group takes their access away at their next sign-in, and SCIM deactivation does so at once). Without role
  mappings, roles stay what admins set, as with the Google login.
- If the provider has mappings with a unit, unit memberships follow the groups the same way.
- An unknown person whose groups map to a role is created with it (the directory vouches for them). Otherwise they
  join as `pending` and admins are notified, unless SELF_REGISTRATION is off (403). ALLOWED_EMAIL_DOMAINS does not
  apply: the hospital's own directory authenticated them.
"""
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core.config import settings
from app.core.security import invalidate_identity
from app.core.tenancy import use_tenant
from app.identity import oidc
from app.identity import service as identity_service
from app.identity.models import IdentityProvider, IdpGroupMapping, User, UserUnit
from app.identity.permissions import PENDING_ROLE
from app.notifications.service import notify_role

# Most privileged first: a person in several mapped groups gets the first of these.
ROLE_PRECEDENCE = ("admin", "physician", "pharmacist", "nurse")


def _provider(row: IdentityProvider) -> oidc.Provider:
    return oidc.Provider(
        provider_id=row.provider_id, tenant_id=row.tenant_id, name=row.name, issuer=row.issuer,
        client_id=row.client_id, authorize_url=row.authorize_url, token_url=row.token_url, jwks_url=row.jwks_url,
        scopes=row.scopes, groups_claim=row.groups_claim, require_mfa=row.require_mfa,
    )


async def providers(db: AsyncSession, tenant_id: Optional[str] = None) -> list[oidc.Provider]:
    """Active providers (database rows, then the environment's), optionally of one tenant."""
    query = select(IdentityProvider).where(IdentityProvider.active.is_(True)).order_by(IdentityProvider.name)
    if tenant_id is not None:
        query = query.where(IdentityProvider.tenant_id == tenant_id)
    found = [_provider(row) for row in await db.scalars(query)]
    env = oidc.env_provider()
    if env and (tenant_id is None or env.tenant_id == tenant_id):
        found.append(env)
    return found


async def provider_by_id(db: AsyncSession, provider_id: str) -> oidc.Provider:
    if provider_id == oidc.ENV_PROVIDER_ID:
        env = oidc.env_provider()
        if env:
            return env
    else:
        row = await db.get(IdentityProvider, provider_id)
        if row is not None and row.active:
            return _provider(row)
    raise HTTPException(status_code=404, detail="Unknown identity provider")


async def provider_for_token(db: AsyncSession, token: str) -> oidc.Provider:
    """The provider whose issuer and client id the token names (then verified with that provider's keys)."""
    iss, aud = oidc.unverified_issuer(token)
    for provider in await providers(db):
        if provider.issuer == iss and provider.client_id == aud:
            return provider
    raise HTTPException(status_code=401, detail="Sign-in token refused: unknown issuer")


async def emergent_login_enabled(db: AsyncSession) -> bool:
    """The Google (Emergent) login is the dev/demo fallback: on while no hospital provider exists, unless forced."""
    if settings.emergent_login != "auto":
        return settings.emergent_login == "true"
    return not await providers(db)


def resolve(mappings: list[IdpGroupMapping], groups: set[str]) -> tuple[Optional[str], Optional[set[str]]]:
    """(role or None if roles aren't managed by groups, unit ids or None if units aren't).

    A managed role with no matching group is `pending`."""
    matched = [m for m in mappings if m.group_name in groups]
    role: Optional[str] = None
    if any(m.role for m in mappings):
        roles = {m.role for m in matched if m.role}
        role = next((r for r in ROLE_PRECEDENCE if r in roles), PENDING_ROLE)
    units = {m.unit_id for m in matched if m.unit_id} if any(m.unit_id for m in mappings) else None
    return role, units


async def _sync_units(db: AsyncSession, user: User, unit_ids: set[str]) -> bool:
    current = set(await db.scalars(select(UserUnit.unit_id).where(UserUnit.user_id == user.user_id)))
    if current == unit_ids:
        return False
    for membership in await db.scalars(select(UserUnit).where(UserUnit.user_id == user.user_id)):
        if membership.unit_id not in unit_ids:
            await db.delete(membership)
    db.add_all(UserUnit(user_id=user.user_id, unit_id=u) for u in sorted(unit_ids - current))
    await db.flush()
    return True


async def sign_in(db: AsyncSession, request: Request, response: Response, provider: oidc.Provider,
                  claims: dict) -> dict:
    email = str(claims["email"]).strip()
    # Looked up before the transaction is bound to the tenant, so an account of another tenant is seen (and refused)
    # rather than colliding with the unique email on insert.
    user = await identity_service.find_user_by_email(db, email)
    await use_tenant(db, provider.tenant_id)
    mappings = list(await db.scalars(
        select(IdpGroupMapping).where(IdpGroupMapping.provider_id == provider.provider_id)))
    role, unit_ids = resolve(mappings, oidc.groups(claims, provider.groups_claim))
    name = oidc.display_name(claims)
    if user is not None and user.tenant_id != provider.tenant_id:
        # Emails are unique across tenants; a person belongs to the hospital whose provider created them.
        raise HTTPException(status_code=403, detail="This account belongs to another hospital")
    if user is not None and user.deactivated_at is not None:
        raise HTTPException(status_code=403, detail="This account has been deactivated by the hospital directory")

    created = user is None
    changed_role = False
    previous = None
    if user is None:
        if role in (None, PENDING_ROLE) and not settings.self_registration:
            raise HTTPException(status_code=403,
                                detail="This account has not been invited. Ask an administrator for access.")
        user = User(email=email, name=name, picture=claims.get("picture"), role=role or PENDING_ROLE)
        db.add(user)
        await db.flush()
        if user.role == PENDING_ROLE:
            await notify_role(db, "admin", "Access Request",
                              f"{user.name} ({user.email}) signed in and is waiting for approval.", "/admin")
    else:
        user.name = name
        user.picture = claims.get("picture")
        if role is not None and user.role != role:
            previous, user.role, changed_role = user.role, role, True
    units_changed = unit_ids is not None and await _sync_units(db, user, unit_ids)
    if changed_role or units_changed:
        invalidate_identity(user_id=user.user_id)
    await db.flush()

    registered = created and user.role == PENDING_ROLE  # audited as REQUEST_ACCESS by start_user_session
    session, refresh_token = await identity_service.start_user_session(db, request, user, registered)
    actor = identity_service.identity_of(user, session)
    if created and not registered:
        await write_audit(db, actor, "SSO_PROVISION_USER", "user", user.user_id,
                          f"provider={provider.provider_id} role={user.role}", request)
    if changed_role:
        await write_audit(db, actor, "SSO_ROLE_SYNC", "user", user.user_id,
                          f"provider={provider.provider_id} {previous} → {user.role}", request)
    if units_changed:
        await write_audit(db, actor, "SSO_UNIT_SYNC", "user", user.user_id,
                          f"provider={provider.provider_id} units={len(unit_ids or ())}", request)
    return await identity_service.token_response(db, response, user, session, refresh_token)
