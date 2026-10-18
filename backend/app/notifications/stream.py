"""Live notification stream over Server-Sent Events (ADR-001 Phase 6, item 21).

There is no message broker on Vercel, and Neon's transaction pooler can't hold a LISTEN, so the stream polls the
notifications table every POLL_SECONDS with a short session of its own (the request's transaction has already
committed by the time the body streams). Each stream lasts `settings.notification_stream_seconds` and then ends;
the client reconnects. The session is re-checked on every poll, so logout or revocation ends the stream.

Events carry only the notification id: the client refetches GET /api/notifications, so no notification text
(which can name a patient) travels over the stream.
"""
import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import utcnow
from app.core.security import CurrentUser, load_identity
from app.core.tenancy import use_tenant
from app.notifications.models import Notification

POLL_SECONDS = 5.0
HEARTBEAT_SECONDS = 15.0


def sse(event: str, data: dict, event_id: str | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def notification_events(
    user: CurrentUser,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    duration: float,
    poll: float = POLL_SECONDS,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames: `ready`, then one `notification` per new row for this user, then `end`."""
    cursor: datetime = utcnow()
    seen_at_cursor: set[str] = set()
    deadline = time.monotonic() + duration
    last_sent = time.monotonic()
    # The client invalidates on `ready`, which covers anything created while it was disconnected.
    yield sse("ready", {"retry_ms": int(poll * 1000)})

    while time.monotonic() < deadline:
        await asyncio.sleep(min(poll, max(deadline - time.monotonic(), 0)))
        if is_disconnected and await is_disconnected():
            return
        async with session_factory() as db:
            await use_tenant(db, user.tenant_id)
            if await load_identity(db, user.user_id, user.session_id) is None:
                yield sse("end", {"reason": "session_inactive"})
                return
            rows = (await db.execute(
                select(Notification.notification_id, Notification.created_at)
                .where(Notification.user_id == user.user_id, Notification.created_at >= cursor)
                .order_by(Notification.created_at)
                .limit(100)
            )).all()
        for notification_id, created_at in rows:
            if notification_id in seen_at_cursor:
                continue
            if created_at > cursor:
                cursor, seen_at_cursor = created_at, set()
            seen_at_cursor.add(notification_id)
            last_sent = time.monotonic()
            yield sse("notification", {"notification_id": notification_id}, notification_id)
        if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
            last_sent = time.monotonic()
            yield ": keep-alive\n\n"
    yield sse("end", {"reason": "timeout"})
