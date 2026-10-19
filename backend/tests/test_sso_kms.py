"""ADR-002 items 2 and 5, code seams: OIDC sign-in (hospital SSO) and the KMS key provider for patient data keys.

Neither runs against a real identity provider or AWS here: the JWKS and the KMS client are replaced by local fakes.
"""
import dataclasses
import json
import os
import time

import jwt
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import crypto, kms
from app.identity import oidc
from app.identity import router as identity_router

pytestmark = pytest.mark.anyio

ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_TEST"
CLIENT_ID = "client-123"
NONCE = "n" * 32


# ---------- OIDC ----------

class FakeJwks:
    """Stands in for PyJWKClient: serves the public half of `keys` by `kid`."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]):
        self.keys = keys

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.PyJWKClientError("no such key")
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
        return jwt.PyJWK({**jwk, "kid": kid, "alg": "RS256"})


@pytest.fixture
def idp(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    configured = dataclasses.replace(oidc.settings, oidc_issuer=ISSUER, oidc_client_id=CLIENT_ID)
    monkeypatch.setattr(oidc, "settings", configured)
    monkeypatch.setattr(identity_router, "settings", dataclasses.replace(identity_router.settings,
                                                                         oidc_issuer=ISSUER, oidc_client_id=CLIENT_ID))
    monkeypatch.setattr(oidc, "jwks_client", lambda: FakeJwks({"k1": key}))

    def issue(signing_key=key, kid="k1", **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "abc", "iat": now, "exp": now + 300, "nonce": NONCE,
                  "token_use": "id", "email": "Dr.Ward@hospital.example", "email_verified": True,
                  "given_name": "Ada", "family_name": "Ward", **overrides}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": kid})

    return issue


async def test_oidc_is_off_unless_configured(client):
    assert (await client.get("/api/auth/config")).json() == {"oidc": None}
    resp = await client.post("/api/auth/oidc", json={"id_token": "x", "nonce": NONCE})
    assert resp.status_code == 404


async def test_oidc_sign_in_matches_invited_user(client, idp, make_user, db):
    _, admin = await make_user("admin")
    resp = await client.post("/api/users", json={"email": "dr.ward@hospital.example", "role": "physician"},
                             headers=admin)
    assert resp.status_code == 200
    assert (await client.get("/api/auth/config")).json() == {"oidc": {"issuer": ISSUER, "client_id": CLIENT_ID}}

    resp = await client.post("/api/auth/oidc", json={"id_token": idp(), "nonce": NONCE})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["role"] == "physician" and body["user"]["name"] == "Ada Ward"
    assert "df_refresh" in resp.headers.get("set-cookie", "")
    bearer = {"Authorization": f"Bearer {body['token']}"}
    assert (await client.get("/api/patients", headers=bearer)).status_code == 200


async def test_oidc_unknown_user_waits_for_approval(client, idp):
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(email="new@hospital.example"), "nonce": NONCE})
    assert resp.status_code == 200 and resp.json()["user"]["role"] == "pending"


@pytest.mark.parametrize("overrides, reason", [
    ({"iss": "https://evil.example"}, "InvalidIssuerError"),
    ({"aud": "another-client"}, "InvalidAudienceError"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"nonce": "m" * 32}, "nonce mismatch"),
    ({"nonce": None}, "nonce mismatch"),
    ({"token_use": "access"}, "not an ID token"),
    ({"email_verified": False}, "email not verified"),
    ({"email": None}, "no email claim"),
    ({"sub": None}, "MissingRequiredClaimError"),
])
async def test_oidc_refuses_bad_tokens(client, idp, overrides, reason):
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(**overrides), "nonce": NONCE})
    assert resp.status_code == 401
    assert resp.json()["detail"] == f"Sign-in token refused: {reason}"


async def test_oidc_refuses_wrong_signer_and_unknown_key(client, idp):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(signing_key=other), "nonce": NONCE})
    assert resp.status_code == 401 and "InvalidSignatureError" in resp.json()["detail"]
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(kid="k2"), "nonce": NONCE})
    assert resp.status_code == 401 and "signing key not found" in resp.json()["detail"]
    # An HS256 token "signed" with the public key (algorithm confusion) is refused.
    forged = jwt.encode({"iss": ISSUER}, "s" * 32, algorithm="HS256", headers={"kid": "k1"})
    resp = await client.post("/api/auth/oidc", json={"id_token": forged, "nonce": NONCE})
    assert resp.status_code == 401


async def test_oidc_mfa_requirement(client, idp, monkeypatch):
    monkeypatch.setattr(oidc, "settings", dataclasses.replace(oidc.settings, oidc_require_mfa=True))
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(amr=["pwd"]), "nonce": NONCE})
    assert resp.status_code == 401 and "multi-factor" in resp.json()["detail"]
    resp = await client.post("/api/auth/oidc", json={"id_token": idp(amr=["pwd", "mfa"]), "nonce": NONCE})
    assert resp.status_code == 200, resp.text


def test_oidc_settings(monkeypatch):
    from app.core import config

    monkeypatch.setenv("OIDC_ISSUER", ISSUER + "/")
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    loaded = config.load_settings()
    assert loaded.oidc_issuer == ISSUER and loaded.oidc_client_id == CLIENT_ID and not loaded.oidc_require_mfa


# ---------- KMS ----------

class FakeKms:
    """Stands in for a boto3 KMS client: AES-GCM under one key, bound to the encryption context like KMS."""

    class Refused(Exception):
        def __init__(self, code: str):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    def __init__(self, key_id: str = "alias/phi"):
        self.key_id = key_id
        self._aead = AESGCM(os.urandom(32))
        self.calls: list[str] = []

    def encrypt(self, KeyId, Plaintext, EncryptionContext):
        self.calls.append("encrypt")
        assert KeyId == self.key_id
        nonce = os.urandom(12)
        return {"CiphertextBlob": nonce + self._aead.encrypt(nonce, Plaintext, json.dumps(EncryptionContext).encode())}

    def decrypt(self, CiphertextBlob, KeyId, EncryptionContext):
        self.calls.append("decrypt")
        try:
            plain = self._aead.decrypt(CiphertextBlob[:12], CiphertextBlob[12:], json.dumps(EncryptionContext).encode())
        except InvalidTag:
            raise self.Refused("InvalidCiphertextException")
        return {"Plaintext": plain}


def test_kms_provider_wraps_and_caches():
    fake = FakeKms()
    provider = kms.KmsKeyProvider("alias/phi", client=fake)
    dek = os.urandom(32)
    wrapped = provider.wrap(dek)
    assert wrapped.startswith(kms.KMS_PREFIX) and dek not in wrapped
    assert provider.unwrap(wrapped) == dek
    assert provider.unwrap(wrapped) == dek
    assert fake.calls == ["encrypt", "decrypt"]  # the second unwrap came from the cache

    # A ciphertext KMS refuses, and a locally wrapped key, both read as "not this key".
    with pytest.raises(InvalidTag):
        provider.unwrap(kms.KMS_PREFIX + os.urandom(60))
    with pytest.raises(InvalidTag):
        provider.unwrap(crypto.LocalKeyProvider(os.urandom(32)).wrap(dek))

    # Other KMS failures (throttling, access denied) are not swallowed.
    class Down(FakeKms):
        def decrypt(self, **kwargs):
            raise self.Refused("AccessDeniedException")

    with pytest.raises(Exception, match="AccessDenied"):
        kms.KmsKeyProvider("alias/phi", client=Down()).unwrap(wrapped)


def test_kms_cache_is_bounded():
    fake = FakeKms()
    provider = kms.KmsKeyProvider("alias/phi", client=fake, cache_size=2)
    wrapped = [provider.wrap(os.urandom(32)) for _ in range(3)]
    for w in wrapped:
        provider.unwrap(w)
    provider.unwrap(wrapped[0])  # evicted by the third, so KMS is asked again
    assert fake.calls.count("decrypt") == 4


async def test_switch_to_kms_rewraps_local_deks(engine, db, monkeypatch):
    """Moving to KMS is a rotation: the local KEK becomes a retired key until rewrap-deks has moved every row."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core import migrate
    from app.patients.models import Patient

    monkeypatch.setattr(migrate, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    local_kek = os.urandom(32)
    monkeypatch.setattr(crypto, "key_provider", lambda: crypto.RotatingKeyProvider(local_kek))
    patient = Patient(name="Kay Ems", date_of_birth="1950-02-02", admission_date="2026-10-01", room_number="2",
                      diagnosis="Asthma", attending_physician="Dr K")
    db.add(patient)
    await db.commit()
    patient_id = patient.patient_id

    provider = crypto.RotatingKeyProvider(kms.KmsKeyProvider("alias/phi", client=FakeKms()), (local_kek,))
    monkeypatch.setattr(crypto, "key_provider", lambda: provider)
    assert await migrate.rewrap_deks() == {"rewrapped": 1, "already_current": 0, "mrn_reindexed": 0}
    assert await migrate.rewrap_deks() == {"rewrapped": 0, "already_current": 1, "mrn_reindexed": 0}

    db.expire_all()
    moved = await db.get(Patient, patient_id)
    assert moved.phi_dek.startswith(kms.KMS_PREFIX) and moved.name == "Kay Ems"


def test_kms_settings(monkeypatch):
    from app.core import config

    monkeypatch.setenv("PHI_KEY_PROVIDER", "kms")
    monkeypatch.delenv("PHI_KMS_KEY_ID", raising=False)
    with pytest.raises(RuntimeError, match="PHI_KMS_KEY_ID"):
        config.load_settings()
    monkeypatch.setenv("PHI_KMS_KEY_ID", "alias/phi")
    assert config.load_settings().phi_kms_key_id == "alias/phi"
    monkeypatch.setenv("PHI_KEY_PROVIDER", "vault")
    with pytest.raises(RuntimeError, match="PHI_KEY_PROVIDER"):
        config.load_settings()
