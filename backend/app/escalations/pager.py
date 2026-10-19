"""Delivery of escalations outside the app: the hospital's secure-messaging or paging gateway (ADR-002 item 12).

In-app notifications are always sent (escalations/service.py); a `Pager` is the extra, external channel.
`ESCALATION_PAGER` picks one: `none` (default; nothing leaves the app) or `webhook`, a JSON POST to
`ESCALATION_WEBHOOK_URL` signed with HMAC-SHA256 over `<timestamp>.<body>` using `ESCALATION_WEBHOOK_SECRET`.
The gateway should check the signature, refuse timestamps older than a few minutes, and drop repeats of the
`Idempotency-Key` (a run whose commit fails pages again on the next run).

Pages carry no PHI: ids, task category and priority, unit code, minutes overdue and a link. Whoever receives one
signs in to see the patient, which keeps unit scope and the PHI-read audit in force.
"""
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-DischargeFlow-Signature"
TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Page:
    """Everything a page may contain. Adding a field here is adding it to what leaves the hospital's app: no PHI."""

    escalation_id: str
    idempotency_key: str
    tenant_id: str
    task_id: str
    level: int
    priority: str
    category: str
    unit: Optional[str]
    target_role: str
    overdue_minutes: int
    link: str


class Pager(Protocol):
    name: str

    async def send(self, page: Page) -> bool:
        """Deliver one page. True when the gateway accepted it; never raises for delivery failures."""
        ...


class NoopPager:
    name = "none"

    async def send(self, page: Page) -> bool:
        return True


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class WebhookPager:
    name = "webhook"

    def __init__(self, url: str, secret: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = url
        self.secret = secret
        self.transport = transport

    async def send(self, page: Page) -> bool:
        body = json.dumps({"type": "task.escalated", **asdict(page)}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": page.idempotency_key,
            SIGNATURE_HEADER: f"t={timestamp},v1={sign(self.secret, timestamp, body)}",
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            # The exception class only: messages can echo the URL, which may carry a token.
            logger.warning("Escalation page failed", extra={"escalation_id": page.escalation_id,
                                                            "error": type(exc).__name__})
            return False
        if response.is_success:
            return True
        logger.warning("Escalation page refused", extra={"escalation_id": page.escalation_id,
                                                         "status_code": response.status_code})
        return False


_override: Optional[Pager] = None


def get_pager() -> Pager:
    if _override is not None:
        return _override
    if settings.escalation_pager == "webhook":
        assert settings.escalation_webhook_url and settings.escalation_webhook_secret  # checked at settings load
        return WebhookPager(settings.escalation_webhook_url, settings.escalation_webhook_secret)
    return NoopPager()


def set_pager(pager: Optional[Pager]) -> None:
    """Tests: replace the configured pager (None restores it)."""
    global _override
    _override = pager
