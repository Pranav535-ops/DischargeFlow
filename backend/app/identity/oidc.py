"""Hospital single sign-on with OpenID Connect ID tokens (ADR-002 item 2; code seam, not yet run against a real
identity provider).

The intended setup is a Cognito user pool federated with the hospital directory (SAML or OIDC), which enforces MFA
and handles deprovisioning there. The browser runs the authorization-code flow with PKCE against the provider and
posts the resulting ID token, with the nonce it generated, to `POST /api/auth/oidc`. This module checks the token;
the router then signs the person in exactly as the Google login does (same user matching and approval rules).

Checks: RS256 signature by a key from the provider's JWKS (fetched and cached by PyJWT), `iss` equal to
OIDC_ISSUER, `aud` equal to OIDC_CLIENT_ID, `exp`/`iat` present and valid (60 s leeway), `nonce` equal to the one
the browser sent (replay of a token captured elsewhere), `token_use` = `id` when present (Cognito also issues access
tokens under the same keys), `email_verified` not false, and with OIDC_REQUIRE_MFA an `amr` claim naming a second
factor.
"""
import hmac
from functools import lru_cache

import jwt
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 60
# `amr` values (RFC 8176) that show a second factor or a phishing-resistant method.
MFA_METHODS = {"mfa", "otp", "hwk", "swk", "sms", "fpt", "face", "iris", "pin", "sc"}


def enabled() -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Keys are cached for an hour; an unknown `kid` (key rotation at the provider) triggers one refetch.
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def jwks_client() -> jwt.PyJWKClient:
    return _jwks_client(settings.oidc_jwks_url or f"{settings.oidc_issuer}/.well-known/jwks.json")


def _refuse(reason: str) -> HTTPException:
    return HTTPException(status_code=401, detail=f"Sign-in token refused: {reason}")


async def verify_id_token(token: str, nonce: str) -> dict:
    """The verified claims of `token`, or a 401 naming what failed (never the token or its claims)."""
    try:
        # The JWKS fetch is blocking I/O.
        key = await run_in_threadpool(jwks_client().get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token, key.key, algorithms=ALGORITHMS, audience=settings.oidc_client_id, issuer=settings.oidc_issuer,
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
    if settings.oidc_require_mfa and not MFA_METHODS & set(claims.get("amr") or []):
        raise _refuse("multi-factor authentication required")
    return claims


def display_name(claims: dict) -> str:
    given_family = " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
    return claims.get("name") or given_family or claims["email"].split("@")[0]
