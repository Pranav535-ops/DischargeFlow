"""ADR-002 item 2: hospital SSO per tenant (issuer -> tenant, directory groups -> role and units, code + PKCE
exchange) and SCIM 2.0 provisioning. The identity provider is a local fake: an RSA key served as its JWKS, and a
stubbed token endpoint."""
import dataclasses
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.audit.models import AuditLog
from app.core.tenancy import current_tenant_id, use_tenant
from app.identity import oidc, sso
from app.identity.models import AuthSession, IdentityProvider, User, UserUnit
from tests.test_sso_kms import FakeJwks

pytestmark = pytest.mark.anyio

TENANT = "stmary"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_STMARY"
CLIENT_ID = "stmary-client"
NONCE = "n" * 32
VERIFIER = "v" * 64
SCIM = "/api/scim/v2"
SCIM_CT = {"Content-Type": "application/scim+json"}


@pytest.fixture
async def hospital(make_tenant, session_factory, monkeypatch):
    """Tenant `stmary` with a database identity provider (MFA required, the default) and its signing key."""
    await make_tenant(TENANT)
    async with session_factory() as session:
        row = IdentityProvider(tenant_id=TENANT, name="St. Mary's", issuer=ISSUER, client_id=CLIENT_ID,
                               authorize_url="https://stmary.auth.example/oauth2/authorize",
                               token_url="https://stmary.auth.example/oauth2/token", groups_claim="custom:groups")
        session.add(row)
        await session.commit()
        provider_id = row.provider_id
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(oidc, "jwks_client", lambda url: FakeJwks({"k1": key}))

    def issue(**overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "s-1", "iat": now, "exp": now + 300, "nonce": NONCE,
                  "token_use": "id", "email": "rn.lee@stmary.example", "email_verified": True, "name": "Robin Lee",
                  "amr": ["pwd", "mfa"], **overrides}
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256",
                          headers={"kid": "k1"})

    return provider_id, issue


async def _sso(client, provider_id, token):
    return await client.post("/api/auth/oidc", json={"id_token": token, "nonce": NONCE, "provider_id": provider_id})


async def _admin_of(make_user, tenant=TENANT):
    return (await make_user("admin", tenant_id=tenant))[1]


async def _unit(client, admin, code):
    resp = await client.post("/api/units", json={"code": code, "name": f"Ward {code}"}, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()["unit_id"]


# ---------- per-tenant providers ----------

async def test_config_lists_tenant_providers_and_retires_demo_login(client, hospital):
    provider_id, _ = hospital
    config = (await client.get("/api/auth/config")).json()
    assert config["emergent"] is False
    [p] = config["providers"]
    assert p == {"provider_id": provider_id, "tenant_id": TENANT, "name": "St. Mary's", "issuer": ISSUER,
                 "client_id": CLIENT_ID, "authorize_url": "https://stmary.auth.example/oauth2/authorize",
                 "scopes": "openid email profile"}
    # The Emergent session exchange is off while a hospital provider exists (EMERGENT_LOGIN=auto).
    assert (await client.post("/api/auth/session", json={"session_id": "x"})).status_code == 404


async def test_issuer_decides_the_tenant(client, hospital, make_user):
    provider_id, issue = hospital
    resp = await _sso(client, provider_id, issue())
    assert resp.status_code == 200, resp.text
    user = resp.json()["user"]
    assert user["tenant_id"] == TENANT and user["role"] == "pending"
    # The token alone (no provider_id) finds the same provider by issuer and audience.
    resp = await client.post("/api/auth/oidc", json={"id_token": issue(), "nonce": NONCE})
    assert resp.status_code == 200 and resp.json()["user"]["user_id"] == user["user_id"]
    # A token from another issuer can't be passed off as this provider's.
    resp = await _sso(client, provider_id, issue(iss="https://other.example"))
    assert resp.status_code == 401 and "InvalidIssuerError" in resp.json()["detail"]
    resp = await _sso(client, provider_id, issue(aud="other-client"))
    assert resp.status_code == 401 and "InvalidAudienceError" in resp.json()["detail"]
    assert (await _sso(client, "idp_missing", issue())).status_code == 404


async def test_provider_requires_mfa_by_default(client, hospital):
    provider_id, issue = hospital
    resp = await _sso(client, provider_id, issue(amr=["pwd"]))
    assert resp.status_code == 401 and "multi-factor" in resp.json()["detail"]


async def test_account_of_another_hospital_is_refused(client, hospital, make_user, db):
    provider_id, issue = hospital
    other, _ = await make_user("nurse")  # default tenant
    resp = await _sso(client, provider_id, issue(email=other["email"]))
    assert resp.status_code == 403 and "another hospital" in resp.json()["detail"]


async def test_disabled_provider_is_refused(client, hospital, session_factory):
    provider_id, issue = hospital
    async with session_factory() as session:
        (await session.get(IdentityProvider, provider_id)).active = False
        await session.commit()
    assert (await _sso(client, provider_id, issue())).status_code == 404
    assert (await client.get("/api/auth/config")).json() == {"providers": [], "emergent": True}


# ---------- directory groups -> role and units ----------

async def test_groups_map_to_role_and_units_at_every_sign_in(client, hospital, make_user, session_factory):
    provider_id, issue = hospital
    admin = await _admin_of(make_user)
    unit_4w = await _unit(client, admin, "4W")
    unit_5e = await _unit(client, admin, "5E")
    for body in ({"group_name": "DF-Physicians", "role": "physician"}, {"group_name": "DF-Nurses", "role": "nurse"},
                 {"group_name": "Ward-4W", "unit_id": unit_4w}, {"group_name": "Ward-5E", "unit_id": unit_5e}):
        resp = await client.post(f"/api/identity-providers/{provider_id}/mappings", json=body, headers=admin)
        assert resp.status_code == 200, resp.text
    listed = (await client.get("/api/identity-providers", headers=admin)).json()
    assert [p["provider_id"] for p in listed] == [provider_id] and len(listed[0]["mappings"]) == 4

    # A stranger in a mapped group is provisioned with the most privileged mapped role and the mapped units.
    resp = await _sso(client, provider_id, issue(**{"custom:groups": "[DF-Nurses, DF-Physicians, Ward-4W]"}))
    assert resp.status_code == 200, resp.text
    body = resp.json()["user"]
    assert body["role"] == "physician" and body["unit_ids"] == [unit_4w]
    bearer = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert (await client.get("/api/patients", headers=bearer)).status_code == 200

    # The directory moves them: the next sign-in follows.
    resp = await _sso(client, provider_id, issue(**{"custom:groups": ["DF-Nurses", "Ward-5E"]}))
    assert resp.json()["user"]["role"] == "nurse" and resp.json()["user"]["unit_ids"] == [unit_5e]
    # Out of every mapped group: no role, no units.
    resp = await _sso(client, provider_id, issue(**{"custom:groups": ["Cafeteria"]}))
    assert resp.json()["user"]["role"] == "pending" and resp.json()["user"]["unit_ids"] == []

    token = current_tenant_id.set(TENANT)
    try:
        async with session_factory() as session:
            await use_tenant(session, TENANT)
            actions = [a.action for a in await session.scalars(select(AuditLog).order_by(AuditLog.seq))]
    finally:
        current_tenant_id.reset(token)
    assert "SSO_PROVISION_USER" in actions and actions.count("SSO_ROLE_SYNC") == 2
    assert actions.count("SSO_UNIT_SYNC") == 3  # provisioned onto 4W, moved to 5E, removed


async def test_mapping_validation_and_tenant_boundary(client, hospital, make_user):
    provider_id, _ = hospital
    admin = await _admin_of(make_user)
    url = f"/api/identity-providers/{provider_id}/mappings"
    assert (await client.post(url, json={"group_name": "G"}, headers=admin)).status_code == 400
    assert (await client.post(url, json={"group_name": "G", "role": "interface"}, headers=admin)).status_code == 400
    assert (await client.post(url, json={"group_name": "G", "unit_id": "unit_nope"}, headers=admin)).status_code == 400
    created = await client.post(url, json={"group_name": "G", "role": "nurse"}, headers=admin)
    assert (await client.post(url, json={"group_name": "G", "role": "admin"}, headers=admin)).status_code == 409
    # Another hospital's admin can't touch this provider.
    other_admin = await _admin_of(make_user, "default")
    assert (await client.post(url, json={"group_name": "H", "role": "nurse"}, headers=other_admin)).status_code == 404
    assert (await client.get("/api/identity-providers", headers=other_admin)).json() == []
    nurse = (await make_user("nurse", tenant_id=TENANT))[1]
    assert (await client.get("/api/identity-providers", headers=nurse)).status_code == 403
    mapping_id = created.json()["mapping_id"]
    resp = await client.delete(f"{url}/{mapping_id}", headers=admin)
    assert resp.status_code == 200
    assert (await client.delete(f"{url}/{mapping_id}", headers=admin)).status_code == 404


async def test_unmapped_stranger_waits_unless_invite_only(client, hospital, monkeypatch):
    provider_id, issue = hospital
    monkeypatch.setattr(sso, "settings", dataclasses.replace(sso.settings, self_registration=False))
    resp = await _sso(client, provider_id, issue())
    assert resp.status_code == 403


# ---------- authorization code + PKCE ----------

async def test_code_exchange(client, hospital, monkeypatch):
    provider_id, issue = hospital
    sent = {}

    async def fake_post(url, form):
        sent.update(url=url, form=form)
        if form["code"] == "bad":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "nope"})
        return httpx.Response(200, json={"id_token": issue(), "access_token": "a", "token_type": "Bearer"})

    monkeypatch.setattr(oidc, "post_token_request", fake_post)
    body = {"provider_id": provider_id, "code": "abc", "code_verifier": VERIFIER,
            "redirect_uri": "https://app.example/auth/sso/callback", "nonce": NONCE}
    resp = await client.post("/api/auth/oidc/callback", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["tenant_id"] == TENANT and "df_refresh" in resp.headers["set-cookie"]
    assert sent["url"] == "https://stmary.auth.example/oauth2/token"
    assert sent["form"] == {"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": "abc",
                            "redirect_uri": body["redirect_uri"], "code_verifier": VERIFIER}

    resp = await client.post("/api/auth/oidc/callback", json={**body, "code": "bad"})
    assert resp.status_code == 401 and resp.json()["detail"].endswith("invalid_grant")
    # The nonce bound into the ID token must match the one this browser generated.
    resp = await client.post("/api/auth/oidc/callback", json={**body, "nonce": "m" * 32})
    assert resp.status_code == 401 and "nonce" in resp.json()["detail"]
    resp = await client.post("/api/auth/oidc/callback", json={**body, "code_verifier": "short"})
    assert resp.status_code == 422


# ---------- SCIM ----------

async def _scim_key(client, make_user, tenant="default"):
    admin = await _admin_of(make_user, tenant)
    resp = await client.post("/api/integrations/clients", json={"name": "Entra provisioning"}, headers=admin)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['api_key']}"}, admin


def _user_body(email="ada.ward@hospital.example", **extra):
    return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": email,
            "name": {"givenName": "Ada", "familyName": "Ward"}, "externalId": "ext-1", "active": True, **extra}


def _patch(*ops):
    return {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": list(ops)}


async def test_scim_requires_a_provisioning_key(client, make_user):
    assert (await client.get(f"{SCIM}/Users")).status_code == 401
    _, admin = await make_user("admin")
    # A person's access token is not an API key.
    assert (await client.get(f"{SCIM}/Users", headers=admin)).status_code == 401
    key, _ = await _scim_key(client, make_user)
    resp = await client.get(f"{SCIM}/ServiceProviderConfig", headers=key)
    assert resp.status_code == 200 and resp.json()["patch"]["supported"] is True
    # X-API-Key works too.
    raw = key["Authorization"][7:]
    assert (await client.get(f"{SCIM}/Users", headers={"X-API-Key": raw})).status_code == 200


async def test_scim_create_get_list(client, make_user, make_tenant):
    key, admin = await _scim_key(client, make_user)
    resp = await client.post(f"{SCIM}/Users", json=_user_body(), headers={**key, **SCIM_CT})
    assert resp.status_code == 201, resp.text
    assert resp.headers["content-type"].startswith("application/scim+json")
    created = resp.json()
    assert created["userName"] == "ada.ward@hospital.example" and created["displayName"] == "Ada Ward"
    assert created["active"] is True and created["externalId"] == "ext-1"
    assert created["meta"]["location"].endswith(f"/api/scim/v2/Users/{created['id']}")

    # Provisioned accounts wait for a role (group mapping at SSO sign-in, or an admin).
    users = (await client.get("/api/users", headers=admin)).json()
    assert next(u for u in users if u["user_id"] == created["id"])["role"] == "pending"

    dup = await client.post(f"{SCIM}/Users", json=_user_body("ADA.WARD@hospital.example"), headers=key)
    assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"
    bad = await client.post(f"{SCIM}/Users", json={"userName": "x@y.z"}, headers=key)
    assert bad.status_code == 400 and bad.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert (await client.post(f"{SCIM}/Users", json=_user_body("not-an-email"), headers=key)).status_code == 400

    got = await client.get(f"{SCIM}/Users/{created['id']}", headers=key)
    assert got.status_code == 200 and got.json()["id"] == created["id"]
    assert (await client.get(f"{SCIM}/Users/user_missing", headers=key)).status_code == 404

    found = (await client.get(f"{SCIM}/Users", params={"filter": 'userName eq "Ada.Ward@Hospital.example"'},
                              headers=key)).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == created["id"]
    found = (await client.get(f"{SCIM}/Users", params={"filter": 'externalId eq "ext-1"'}, headers=key)).json()
    assert found["totalResults"] == 1
    none = (await client.get(f"{SCIM}/Users", params={"filter": 'userName eq "nobody@x.example"'},
                             headers=key)).json()
    assert none["totalResults"] == 0 and none["Resources"] == []
    resp = await client.get(f"{SCIM}/Users", params={"filter": 'name.givenName sw "A"'}, headers=key)
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidFilter"
    page = (await client.get(f"{SCIM}/Users", params={"startIndex": 1, "count": 1}, headers=key)).json()
    assert page["itemsPerPage"] == 1 and page["totalResults"] >= 2  # the admin and Ada

    # Another hospital's connector sees none of it.
    other_key, _ = await _scim_key(client, make_user, await make_tenant("hospital_b"))
    assert (await client.get(f"{SCIM}/Users/{created['id']}", headers=other_key)).status_code == 404


async def test_scim_deactivation_revokes_sessions(client, make_user, session_factory):
    key, _ = await _scim_key(client, make_user)
    nurse, nurse_headers = await make_user("nurse")
    assert (await client.get("/api/patients", headers=nurse_headers)).status_code == 200  # identity now cached

    url = f"{SCIM}/Users/{nurse['user_id']}"
    # Entra ID's shape: no path, "False" as a string.
    resp = await client.patch(url, json=_patch({"op": "Replace", "value": {"active": "False"}}), headers=key)
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False
    assert (await client.get("/api/patients", headers=nurse_headers)).status_code == 401
    async with session_factory() as session:
        sessions = list(await session.scalars(select(AuthSession).where(AuthSession.user_id == nurse["user_id"])))
        assert sessions and all(s.revoked_at and s.revoked_reason == "deprovisioned" for s in sessions)
        assert (await session.get(User, nurse["user_id"])).deactivated_at is not None

    # Reactivation lets them sign in again; the revoked sessions stay revoked.
    resp = await client.patch(url, json=_patch({"op": "replace", "path": "active", "value": True}), headers=key)
    assert resp.json()["active"] is True
    assert (await client.get("/api/patients", headers=nurse_headers)).status_code == 401

    resp = await client.patch(url, json=_patch({"op": "remove", "path": "active"}), headers=key)
    assert resp.status_code == 400
    resp = await client.patch(url, json=_patch({"op": "replace", "path": "roles", "value": "admin"}), headers=key)
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidPath"
    resp = await client.patch(url, json=_patch({"op": "replace", "path": "active", "value": "maybe"}), headers=key)
    assert resp.status_code == 400
    resp = await client.patch(url, json={"Operations": []}, headers=key)
    assert resp.status_code == 400


async def test_deactivated_account_cannot_sign_in_or_refresh(client, hospital, make_user, session_factory):
    provider_id, issue = hospital
    key, _ = await _scim_key(client, make_user, TENANT)
    resp = await _sso(client, provider_id, issue())
    assert resp.status_code == 200
    user_id = resp.json()["user"]["user_id"]
    refresh_cookie = resp.cookies.get("df_refresh")
    assert refresh_cookie

    resp = await client.patch(f"{SCIM}/Users/{user_id}", json=_patch({"op": "replace", "path": "active",
                                                                       "value": False}), headers=key)
    assert resp.status_code == 200
    client.cookies.set("df_refresh", refresh_cookie, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401
    resp = await _sso(client, provider_id, issue())
    assert resp.status_code == 403 and "deactivated" in resp.json()["detail"]

    token = current_tenant_id.set(TENANT)
    try:
        async with session_factory() as session:
            await use_tenant(session, TENANT)
            actions = [a.action for a in await session.scalars(select(AuditLog).where(AuditLog.resource_id == user_id))]
            assert "SCIM_DEACTIVATE_USER" in actions
            assert not list(await session.scalars(select(UserUnit).where(UserUnit.user_id == user_id)))
    finally:
        current_tenant_id.reset(token)


async def test_scim_create_inactive_and_patch_profile(client, make_user):
    key, _ = await _scim_key(client, make_user)
    resp = await client.post(f"{SCIM}/Users", json=_user_body("sam@hospital.example", active=False), headers=key)
    assert resp.status_code == 201 and resp.json()["active"] is False
    user_id = resp.json()["id"]
    resp = await client.patch(f"{SCIM}/Users/{user_id}", json=_patch(
        {"op": "replace", "path": "displayName", "value": "Sam Okafor"},
        {"op": "replace", "path": "externalId", "value": "ext-2"}), headers=key)
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "Sam Okafor" and resp.json()["externalId"] == "ext-2"


async def test_user_provision_permission_is_migrated():
    from app.identity.permissions import DEFAULT_ROLE_PERMISSIONS, INTEGRATION_ROLE

    assert DEFAULT_ROLE_PERMISSIONS["user:provision"] == (INTEGRATION_ROLE,)


async def test_scim_deprovisioning_works_during_downtime(client, make_user):
    key, admin = await _scim_key(client, make_user)
    nurse, _ = await make_user("nurse")
    resp = await client.post("/api/downtime/start", json={"reason": "Database maintenance"}, headers=admin)
    assert resp.status_code == 200, resp.text
    resp = await client.patch(f"{SCIM}/Users/{nurse['user_id']}",
                              json=_patch({"op": "replace", "path": "active", "value": False}), headers=key)
    assert resp.status_code == 200 and resp.json()["active"] is False
