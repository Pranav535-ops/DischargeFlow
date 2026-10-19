"""Per-process LISTEN hub for the notification stream (ADR-002 item 21; finding 9, design change 4).

An AFTER INSERT trigger on `notifications` (migration 0016) calls
`pg_notify('notifications', '<tenant_id>:<user_id>:<notification_id>')`. Postgres delivers a NOTIFY only when the
inserting transaction commits, and never for one that rolls back, so a stream can't announce a notification that
does not exist. The hub holds ONE dedicated asyncpg connection per process with `LISTEN notifications` (a direct
connection: a transaction pooler would drop the LISTEN) and hands each id to the open streams of that tenant and
user. It reconnects with backoff; after a reconnect every subscriber gets a resync marker (None), because anything
committed while the connection was down was not heard, and the stream tells the client to refetch.

Only ids travel on the channel, never titles or messages (they can name a patient).
"""
import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

import asyncpg

from app.core.config import settings
from app.core.db import direct_connection_dsn

logger = logging.getLogger(__name__)

CHANNEL = "notifications"
QUEUE_SIZE = 100
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
# A LISTEN connection that silently dies (dropped TCP, failover) fires no termination callback, so it is pinged.
PING_SECONDS = 30.0

# None in a subscriber's queue: notifications may have been missed, refetch the list.
Item = str | None
Key = tuple[str, str]


class NotificationHub:
    def __init__(self, dsn: str, connect_args: dict | None = None, *,
                 connect: Callable[..., Any] = asyncpg.connect) -> None:
        self._dsn = dsn
        self._connect_args = connect_args or {}
        self._connect = connect
        self._subscribers: dict[Key, set[asyncio.Queue[Item]]] = {}
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run(), name="notification-hub")

    async def wait_connected(self, timeout: float) -> bool:
        self.start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected.clear()

    @contextlib.contextmanager
    def subscription(self, tenant_id: str, user_id: str) -> Iterator[asyncio.Queue[Item]]:
        queue: asyncio.Queue[Item] = asyncio.Queue(QUEUE_SIZE)
        key = (tenant_id, user_id)
        self._subscribers.setdefault(key, set()).add(queue)
        try:
            yield queue
        finally:
            queues = self._subscribers.get(key)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    del self._subscribers[key]

    def subscriber_count(self) -> int:
        return sum(len(q) for q in self._subscribers.values())

    def dispatch(self, payload: str) -> None:
        """Fan one NOTIFY payload out to the streams of exactly that tenant and user."""
        parts = payload.rsplit(":", 2)
        if len(parts) != 3 or not all(parts):
            logger.warning("Ignoring malformed notification payload")
            return
        tenant_id, user_id, notification_id = parts
        for queue in self._subscribers.get((tenant_id, user_id), ()):
            _offer(queue, notification_id)

    def _resync_all(self) -> None:
        for queues in self._subscribers.values():
            for queue in queues:
                _offer(queue, None)

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        self.dispatch(payload)

    async def _run(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        heard_before = False
        while True:
            try:
                conn = await self._connect(self._dsn, **self._connect_args)
            except Exception as exc:  # unreachable, refused, auth: retry with backoff
                logger.warning("Notification LISTEN connection failed; retrying",
                               extra={"error": type(exc).__name__, "retry_seconds": delay})
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
                continue
            lost = asyncio.Event()
            try:
                conn.add_termination_listener(lambda _c: lost.set())
                await conn.add_listener(CHANNEL, self._on_notify)
                self._connected.set()
                if heard_before:
                    self._resync_all()
                heard_before = True
                delay = RECONNECT_MIN_SECONDS
                logger.info("Notification LISTEN connected")
                while not lost.is_set():
                    try:
                        await asyncio.wait_for(lost.wait(), PING_SECONDS)
                    except TimeoutError:
                        await asyncio.wait_for(conn.execute("SELECT 1"), 10)
                logger.warning("Notification LISTEN connection closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Notification LISTEN connection lost; reconnecting", extra={"error": type(exc).__name__})
            finally:
                self._connected.clear()
                with contextlib.suppress(Exception):
                    conn.terminate()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)


def _offer(queue: asyncio.Queue[Item], item: Item) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # A stream this far behind refetches the whole list anyway: replace the backlog with one resync marker.
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)


_hub: NotificationHub | None = None
_unavailable_logged = False


def get_hub() -> NotificationHub | None:
    """The process's hub when NOTIFICATION_TRANSPORT=listen and the database can LISTEN, else None (poll)."""
    global _hub, _unavailable_logged
    if settings.notification_transport != "listen":
        return None
    loop = asyncio.get_running_loop()
    if _hub is not None and _hub._loop not in (None, loop):
        _hub = None  # a new event loop (tests, a restarted server): the old connection belongs to the old one
    if _hub is None:
        try:
            dsn, connect_args = direct_connection_dsn(settings.database_direct_url or settings.database_url)
        except ValueError as exc:
            if not _unavailable_logged:
                logger.warning("NOTIFICATION_TRANSPORT=listen is unavailable (%s); the stream polls", exc)
                _unavailable_logged = True
            return None
        _hub = NotificationHub(dsn, connect_args)
    _hub.start()
    return _hub


async def stop_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.stop()
        _hub = None
