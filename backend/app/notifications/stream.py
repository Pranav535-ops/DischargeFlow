"""Live notification stream over Server-Sent Events (ADR-001 Phase 6, item 21; ADR-002 item 21).

Two transports, chosen by NOTIFICATION_TRANSPORT:

- `poll` (default; Vercel): there is no long-lived process, and Neon's transaction pooler can't hold a LISTEN, so the
  stream polls the notifications table every POLL_SECONDS with a short session of its own (the request's
  transaction has already committed by the time the body streams), re-checking the auth session on every poll.
- `listen` (long-running hosts): ids arrive from the process's LISTEN hub (`hub.py`) as the inserting transaction
  commits, with no per-stream query. The auth session is re-checked every SESSION_CHECK_SECONDS. If the hub can't
  connect within CONNECT_WAIT_SECONDS, the stream falls back to polling, so a database outage degrades to latency.

Each stream lasts `settings.notification_stream_seconds` and then ends; the client reconnects. Logout or revocation
ends the stream at the next session check.

Events carry only the notification id: the client refetches GET /api/notifications, so no notification text
(which can name a patient) travels over the stream.
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import utcnow
from app.core.security import CurrentUser, load_identity
from app.core.tenancy import use_tenant
from app.notifications.hub import NotificationHub
from app.notifications.models import Notification

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
HEARTBEAT_SECONDS = 15.0
SESSION_CHECK_SECONDS = 15.0
CONNECT_WAIT_SECONDS = 3.0


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
    hub: NotificationHub | None = None,
    session_check: float = SESSION_CHECK_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames: `ready`, then one `notification` per new row for this user, then `end`."""
    if hub is not None:
        if await hub.wait_connected(CONNECT_WAIT_SECONDS):
            async for frame in _listen_events(user, session_factory, hub, duration=duration, poll=poll,
                                              session_check=session_check, is_disconnected=is_disconnected):
                yield frame
            return
        logger.warning("Notification hub not connected; this stream polls")
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


async def _session_active(user: CurrentUser, session_factory: async_sessionmaker[AsyncSession]) -> bool:
    async with session_factory() as db:
        await use_tenant(db, user.tenant_id)
        return await load_identity(db, user.user_id, user.session_id) is not None


async def _listen_events(
    user: CurrentUser,
    session_factory: async_sessionmaker[AsyncSession],
    hub: NotificationHub,
    *,
    duration: float,
    poll: float,
    session_check: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None,
) -> AsyncIterator[str]:
    now = time.monotonic()
    deadline = now + duration
    next_check = now + session_check
    last_sent = now
    # Subscribe before `ready`: the client refetches on `ready`, and anything committed after it arrives here.
    with hub.subscription(user.tenant_id, user.user_id) as queue:
        yield sse("ready", {"retry_ms": int(poll * 1000)})
        while (now := time.monotonic()) < deadline:
            timeout = max(min(deadline - now, next_check - now, last_sent + HEARTBEAT_SECONDS - now), 0)
            try:
                item: str | None | bool = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                item = False
            if is_disconnected and await is_disconnected():
                return
            if item is None:
                # The hub reconnected or this stream fell behind: tell the client to refetch the list.
                last_sent = time.monotonic()
                yield sse("ready", {"retry_ms": int(poll * 1000)})
            elif isinstance(item, str):
                last_sent = time.monotonic()
                yield sse("notification", {"notification_id": item}, item)
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + session_check
                if not await _session_active(user, session_factory):
                    yield sse("end", {"reason": "session_inactive"})
                    return
            if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                last_sent = time.monotonic()
                yield ": keep-alive\n\n"
    yield sse("end", {"reason": "timeout"})
