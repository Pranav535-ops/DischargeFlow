"""Envelope encryption for patient identifiers (PHI).

Each patient row gets its own random 256-bit data key (DEK). The DEK is stored wrapped by the key-encryption key
(KEK) from PHI_KEK, and each field is encrypted with AES-256-GCM using the DEK, with the patient id and field name as
associated data so a ciphertext cannot be moved to another row or column.

Destroying a patient's wrapped DEK makes its ciphertext unreadable ("crypto-shredding"). With a KEK held in app
config, copies of the wrapped DEK in older database backups remain decryptable; moving the KEK to a KMS with key
deletion (ADR-001, AWS phase) closes that gap. `KeyProvider` is the seam for that change.
"""
import base64
import hashlib
import hmac
import os
from functools import lru_cache
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

PREFIX = "enc:v1:"
_NONCE = 12


class KeyProvider(Protocol):
    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    """Wraps DEKs with an AES-256-GCM KEK held in application config."""

    def __init__(self, kek: bytes):
        self._aead = AESGCM(kek)

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aead.encrypt(nonce, dek, b"dischargeflow-dek")

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._aead.decrypt(wrapped[:_NONCE], wrapped[_NONCE:], b"dischargeflow-dek")


class RotatingKeyProvider:
    """Wraps with the current KEK; unwraps with the current one or any retired one (PHI_KEK_PREVIOUS).

    AES-GCM authentication tells the keys apart, so wrapped DEKs need no key id. `rewrap` reports whether a DEK
    was wrapped by a retired key and returns it wrapped by the current one (used by `app.cli rewrap-deks`).
    """

    def __init__(self, current: bytes, previous: tuple[bytes, ...] = ()):
        self._current = LocalKeyProvider(current)
        self._previous = [LocalKeyProvider(k) for k in previous]

    def wrap(self, dek: bytes) -> bytes:
        return self._current.wrap(dek)

    def unwrap(self, wrapped: bytes) -> bytes:
        return self.rewrap(wrapped)[0]

    def rewrap(self, wrapped: bytes) -> tuple[bytes, bytes | None]:
        """(dek, newly wrapped DEK or None when it is already wrapped by the current KEK)."""
        try:
            return self._current.unwrap(wrapped), None
        except InvalidTag:
            pass
        for old in self._previous:
            try:
                dek = old.unwrap(wrapped)
            except InvalidTag:
                continue
            return dek, self._current.wrap(dek)
        raise InvalidTag("DEK is not wrapped by PHI_KEK or any key in PHI_KEK_PREVIOUS")


@lru_cache(maxsize=1)
def key_provider() -> RotatingKeyProvider:
    return RotatingKeyProvider(settings.phi_kek, settings.phi_kek_previous)


def new_wrapped_dek() -> bytes:
    return key_provider().wrap(AESGCM.generate_key(bit_length=256))


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_field(wrapped_dek: bytes, plaintext: str, aad: str) -> str:
    dek = key_provider().unwrap(wrapped_dek)
    nonce = os.urandom(_NONCE)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(wrapped_dek: bytes, token: str, aad: str) -> str:
    raw = base64.b64decode(token[len(PREFIX):])
    dek = key_provider().unwrap(wrapped_dek)
    return AESGCM(dek).decrypt(raw[:_NONCE], raw[_NONCE:], aad.encode("utf-8")).decode("utf-8")


# ---------- blind indexes ----------
# Encrypted fields can't be queried, so a field that must be looked up by value (the MRN that HL7 messages carry)
# also stores a keyed HMAC of its normalized value. The HMAC key is derived from the KEK with HKDF, so no extra
# secret is needed; without the KEK the index can't be recomputed or brute-forced from a dictionary of MRNs.


def _index_key(kek: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=f"dischargeflow-blind-index:{purpose}".encode()).derive(kek)


@lru_cache(maxsize=16)
def _index_keys(purpose: str) -> tuple[bytes, ...]:
    """The current KEK's index key first, then those of retired KEKs (PHI_KEK_PREVIOUS)."""
    return tuple(_index_key(k, purpose) for k in (settings.phi_kek, *settings.phi_kek_previous))


def _index(key: bytes, parts: tuple[str, ...]) -> str:
    message = "\x1f".join(parts).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def blind_index(purpose: str, *parts: str) -> str:
    """HMAC-SHA256 of `parts` under the current KEK's index key, as stored on new rows."""
    return _index(_index_keys(purpose)[0], parts)


def blind_index_candidates(purpose: str, *parts: str) -> list[str]:
    """Every index value `parts` may be stored under: rows written before a KEK rotation keep the old one until
    `app.cli rewrap-deks` recomputes them, so lookups match on any of these."""
    return [_index(key, parts) for key in _index_keys(purpose)]
