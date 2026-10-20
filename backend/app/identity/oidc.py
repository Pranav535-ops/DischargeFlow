"""Hospital single sign-on with OpenID Connect (ADR-002 item 2; not yet run against a real hospital directory).

The intended setup is a Cognito user pool per environment (`infra/modules/cognito`), federated with the hospital
directory over SAML or OIDC, which enforces MFA. Each hospital (tenant) has an `identity_providers` row, or the
deployment configures one with OIDC_* variables (provider id `env`, default tenant). The browser runs the
authorization-code flow with PKCE against the provider and posts the code, its verifier and the nonce it generated
to `POST /api/auth/oidc/callback`; the server redeems the code at the token endpoint (a public client: no secret) and
checks the ID token here. `POST /api/auth/oidc` still accepts an ID token directly.

Checks: RS256 signature by a key from the provider's JWKS (fetched and cached by PyJWT), `iss` equal to the
provider's issuer, `aud` equal to its client id, `exp`/`iat` present and valid (60 s leeway), `nonce` equal to the
one the browser sent (replay of a token captured elsewhere), `token_use` = `id` when present (Cognito also issues
access tokens under the same keys), `email_verified` not false, and when the provider requires MFA an `amr` claim
naming a second factor.
"""
import hmac
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import httpx
import jwt
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 60
# `amr` values (RFC 8176) that show a second factor or a phishing-resistant method.
MFA_METHODS = {"mfa", "otp", "hwk", "swk", "sms", "fpt", "face", "iris", "pin", "sc"}
ENV_PROVIDER_ID = "env"


@dataclass(frozen=True)
class Provider:
    """An identity provider as sign-in uses it: an `identity_providers` row or the OIDC_* environment."""

    provider_id: str
    tenant_id: str
    name: str
    issuer: str
    client_id: str
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    jwks_url: Optional[str] = None
    scopes: str = "openid email profile"
    groups_claim: str = "groups"
    require_mfa: bool = False

    @property
    def jwks(self) -> str:
        return self.jwks_url or f"{self.issuer}/.well-known/jwks.json"

    def public(self) -> dict:
        """What the login page needs to start the redirect (all public values)."""
        return {"provider_id": self.provider_id, "tenant_id": self.tenant_id, "name": self.name,
                "issuer": self.issuer, "client_id": self.client_id, "authorize_url": self.authorize_url,
                "scopes": self.scopes}


def enabled() -> bool:
    """Whether the environment configures a provider (OIDC_ISSUER and OIDC_CLIENT_ID)."""
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def env_provider() -> Optional[Provider]:
    if not settings.oidc_issuer or not settings.oidc_client_id:
        return None
    return Provider(
        provider_id=ENV_PROVIDER_ID, tenant_id=settings.default_tenant_id, name=settings.oidc_provider_name,
        issuer=settings.oidc_issuer, client_id=settings.oidc_client_id, authorize_url=settings.oidc_authorize_url,
        token_url=settings.oidc_token_url, jwks_url=settings.oidc_jwks_url, groups_claim=settings.oidc_groups_claim,
        require_mfa=settings.oidc_require_mfa,
    )


@lru_cache(maxsize=16)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Keys are cached for an hour; an unknown `kid` (key rotation at the provider) triggers one refetch.
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def jwks_client(url: str) -> jwt.PyJWKClient:
    return _jwks_client(url)


def _refuse(reason: str) -> HTTPException:
    return HTTPException(status_code=401, detail=f"Sign-in token refused: {reason}")


def unverified_issuer(token: str) -> tuple[str, str]:
    """(iss, aud) read without verification, only to pick the provider whose keys then verify the token."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        raise _refuse("malformed")
    aud = claims.get("aud")
    if isinstance(aud, list):
        aud = aud[0] if len(aud) == 1 else ""
    return str(claims.get("iss", "")), str(aud or "")


async def verify_id_token(token: str, nonce: str, provider: Provider) -> dict:
    """The verified claims of `token`, or a 401 naming what failed (never the token or its claims)."""
    try:
        # The JWKS fetch is blocking I/O.
        key = await run_in_threadpool(jwks_client(provider.jwks).get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token, key.key, algorithms=ALGORITHMS, audience=provider.client_id, issuer=provider.issuer,
            leeway=LEEWAY_SECONDS, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWKClientError:
        raise _refuse("signing key not found")
    except jwt.ExpiredSignatureError:
        raise _refuse("expired")
    except jwt.InvalidTokenError as e:
        raise _refuse(type(e).__name__)

    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise _refuse("nonce mismatch")
    if claims.get("token_use", "id") != "id":
        raise _refuse("not an ID token")
    if claims.get("email_verified") in (False, "false"):
        raise _refuse("email not verified")
    if not claims.get("email"):
        raise _refuse("no email claim")
    if provider.require_mfa and not MFA_METHODS & set(claims.get("amr") or []):
        raise _refuse("multi-factor authentication required")
    return claims


async def post_token_request(url: str, form: dict) -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.post(url, data=form, headers={"Accept": "application/json"}, timeout=10.0)


async def exchange_code(provider: Provider, code: str, code_verifier: str, redirect_uri: str) -> str:
    """Redeem an authorization code at the provider's token endpoint and return the ID token.

    A public client: PKCE's code verifier stands in for a client secret, so a code intercepted on the redirect is
    useless without the verifier that stayed in the browser tab that started the sign-in.
    """
    if not provider.token_url:
        raise HTTPException(status_code=400, detail="This identity provider has no token endpoint configured")
    form = {"grant_type": "authorization_code", "client_id": provider.client_id, "code": code,
            "redirect_uri": redirect_uri, "code_verifier": code_verifier}
    try:
        resp = await post_token_request(provider.token_url, form)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="The identity provider could not be reached")
    if resp.status_code != 200:
        # Provider error bodies name the error code only (invalid_grant: expired or reused code, wrong verifier).
        try:
            error = str(resp.json().get("error", "error"))[:60]
        except ValueError:
            error = "error"
        raise HTTPException(status_code=401, detail=f"Sign-in code refused by the identity provider: {error}")
    id_token = resp.json().get("id_token")
    if not id_token:
        raise HTTPException(status_code=401, detail="The identity provider returned no ID token")
    return id_token


def display_name(claims: dict) -> str:
    given_family = " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
    return claims.get("name") or given_family or claims["email"].split("@")[0]


def groups(claims: dict, claim: str) -> set[str]:
    """The directory groups in `claim`: a JSON list, or a comma-separated string (Cognito passes SAML attributes
    through as strings, sometimes bracketed)."""
    raw = claims.get(claim)
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = raw.strip().strip("[]").split(",")
    return {str(g).strip() for g in raw if str(g).strip()}
