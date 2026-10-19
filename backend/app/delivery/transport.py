"""How a delivery reaches its receiver (ADR-002 item 16). An adapter interface, like escalations/pager.py.

- `HttpsTransport`: POSTs the document. A FHIR Bundle goes to `<endpoint url>/Bundle` (a FHIR server's base URL) as
  `application/fhir+json`; a C-CDA document goes to the endpoint URL as `application/xml`. Headers carry an
  `Idempotency-Key` (the delivery id plus the document hash), so a resend after a run whose commit failed can be
  dropped, and with DELIVERY_SIGNING_SECRET an HMAC-SHA256 signature over `<timestamp>.<body>`.
- `MockTransport`: accepts and acknowledges everything without leaving the process. For tests and demos; refused for
  endpoints in production.

Results carry an HTTP status and a short error code, never a response body (it could echo PHI back).
"""
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-DischargeFlow-Signature"
TIMEOUT_SECONDS = 10.0
# Refusals worth retrying; any other 4xx is permanent (the receiver will refuse the same document again).
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class Outgoing:
    delivery_id: str
    url: Optional[str]
    document_format: str
    content_type: str
    body: bytes
    idempotency_key: str


@dataclass(frozen=True)
class Result:
    ok: bool
    acknowledged: bool = False  # the receiver stored it (200/201), not just accepted it for processing (202)
    retryable: bool = True
    http_status: Optional[int] = None
    error: Optional[str] = None
    remote_id: Optional[str] = None


class Transport(Protocol):
    name: str

    async def send(self, outgoing: Outgoing) -> Result:
        """Deliver one document. Never raises for delivery failures."""
        ...


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def target_url(url: str, document_format: str) -> str:
    return f"{url.rstrip('/')}/Bundle" if document_format == "fhir_bundle" else url


class HttpsTransport:
    name = "https"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.transport = transport

    async def send(self, outgoing: Outgoing) -> Result:
        if not outgoing.url:
            return Result(ok=False, retryable=False, error="no_url")
        headers = {"Content-Type": outgoing.content_type, "Accept": "application/fhir+json, application/json",
                   "Idempotency-Key": outgoing.idempotency_key}
        if settings.delivery_signing_secret:
            timestamp = str(int(time.time()))
            headers[SIGNATURE_HEADER] = (f"t={timestamp},"
                                         f"v1={sign(settings.delivery_signing_secret, timestamp, outgoing.body)}")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.post(target_url(outgoing.url, outgoing.document_format),
                                             content=outgoing.body, headers=headers)
        except httpx.TimeoutException:
            return Result(ok=False, error="timeout")
        except httpx.HTTPError as exc:
            # The exception class only: messages can echo the URL.
            logger.warning("Summary delivery failed", extra={"delivery_id": outgoing.delivery_id,
                                                             "error": type(exc).__name__})
            return Result(ok=False, error="connect_error")
        status = response.status_code
        if response.is_success:
            location = response.headers.get("Location") or response.headers.get("Content-Location")
            return Result(ok=True, acknowledged=status != 202, http_status=status,
                          remote_id=location[:300] if location else None)
        retryable = status >= 500 or status in RETRYABLE_STATUSES
        return Result(ok=False, retryable=retryable, http_status=status, error=f"http_{status}")


@dataclass
class MockTransport:
    """Accepts everything (or follows a script of results, for tests) and remembers what it was sent."""

    name: str = "mock"
    script: list[Result] = field(default_factory=list)
    sent: list[Outgoing] = field(default_factory=list)

    async def send(self, outgoing: Outgoing) -> Result:
        self.sent.append(outgoing)
        if self.script:
            return self.script.pop(0)
        return Result(ok=True, acknowledged=True, http_status=201, remote_id=f"mock/{outgoing.delivery_id}")


_mock = MockTransport()
_overrides: dict[str, Transport] = {}


def get_transport(name: str) -> Transport:
    if name in _overrides:
        return _overrides[name]
    if name == "mock":
        return _mock
    return HttpsTransport()


def set_transport(name: str, transport: Optional[Transport]) -> None:
    """Tests: replace a transport (None restores it)."""
    if transport is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = transport
