"""AWS KMS as the key-encryption key for patient data keys (ADR-002 item 5; code seam, not yet run against AWS).

With the KEK in KMS, the application never holds it: wrapping and unwrapping are KMS calls, logged by CloudTrail, and
scheduling the key's deletion makes every wrapped data key, including those in old backups, unreadable. That is what
turns `Patient.shred()` into erasure that survives restores (see `core/crypto.py`).

This is the only module in `app.core` allowed to import boto3 (see `.importlinter`), and it imports it lazily, so
the local key provider needs no AWS SDK configuration.
"""
import threading
import time
from collections import OrderedDict
from typing import Any

from cryptography.exceptions import InvalidTag

# Marks a KMS-wrapped data key. A data key wrapped by the local provider starts with a random nonce, so it can begin
# with these bytes only by chance (2^-32); KMS then refuses it and the rotating provider tries the local keys next.
KMS_PREFIX = b"kms1:"
# Bound to every wrap: KMS refuses to unwrap with a different context, so a ciphertext made for another purpose
# under the same key can't be passed off as a data key.
ENCRYPTION_CONTEXT = {"app": "dischargeflow", "purpose": "phi-dek"}
# KMS errors that mean "not ours / not this key", which the caller treats like a failed AES-GCM tag.
_NOT_THIS_KEY = {"InvalidCiphertextException", "IncorrectKeyException"}


class KmsKeyProvider:
    """`core.crypto.KeyProvider` backed by a KMS symmetric key.

    Every field read unwraps the patient's data key, so unwrapped keys are cached in memory for `cache_seconds`
    (bounded by `cache_size`); without that a patient list would make one KMS call per field. A shredded patient's
    wrapped key no longer exists in the database, so a cached copy can't be asked for again.
    """

    def __init__(self, key_id: str, client: Any = None, region: str | None = None,
                 cache_seconds: float = 300.0, cache_size: int = 4096):
        self._key_id = key_id
        self._client = client
        self._region = region
        self._cache_seconds = cache_seconds
        self._cache_size = cache_size
        self._cache: OrderedDict[bytes, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def _kms(self) -> Any:
        if self._client is None:
            import boto3  # lazy: only deployments that choose PHI_KEY_PROVIDER=kms need AWS credentials

            self._client = boto3.client("kms", region_name=self._region)
        return self._client

    def wrap(self, dek: bytes) -> bytes:
        resp = self._kms().encrypt(KeyId=self._key_id, Plaintext=dek, EncryptionContext=ENCRYPTION_CONTEXT)
        return KMS_PREFIX + resp["CiphertextBlob"]

    def unwrap(self, wrapped: bytes) -> bytes:
        if not wrapped.startswith(KMS_PREFIX):
            raise InvalidTag("data key is not KMS-wrapped")
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(wrapped)
            if hit and hit[0] > now:
                self._cache.move_to_end(wrapped)
                return hit[1]
        try:
            resp = self._kms().decrypt(CiphertextBlob=wrapped[len(KMS_PREFIX):], KeyId=self._key_id,
                                       EncryptionContext=ENCRYPTION_CONTEXT)
        except Exception as e:  # botocore.exceptions.ClientError, matched by code so app.core needs no botocore
            if getattr(e, "response", {}).get("Error", {}).get("Code") in _NOT_THIS_KEY:
                raise InvalidTag("KMS refused the data key") from e
            raise
        dek = resp["Plaintext"]
        with self._lock:
            self._cache[wrapped] = (now + self._cache_seconds, dek)
            self._cache.move_to_end(wrapped)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return dek
