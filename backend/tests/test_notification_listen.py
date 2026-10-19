"""ADR-002 item 21: notification delivery over Postgres LISTEN/NOTIFY instead of per-stream polling."""
import asyncio
import dataclasses
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import update

from app.core import config, rls
from app.core.db import direct_connection_dsn, get_session_factory, utcnow
from app.core.security import CurrentUser, decode_access_token, invalidate_identity
from app.identity.models import AuthSession
from app.main import app
from app.notifications import hub as hub_module
from app.notifications import router as notifications_router
from app.notifications.hub import NotificationHub
from app.notifications.models import Notification
from app.notifications.stream import notification_events
from tests.conftest import TEST_DATABASE_URL, requires_postgres

pytestmark = pytest.mark.anyio
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261019_0016_notification_listen_trigger.py"


def _frame(raw: str) -> tuple[str, str]:
    lines = dict(line.split(": ", 1) for line in raw.strip().splitlines() if not line.startswith(":"))
    return lines.get("event", ""), lines.get("data", "")


async def _identity(user: dict, headers: dict) -> CurrentUser:
    claims = decode_access_token(headers["Authorization"][7:])
    return CurrentUser(user_id=user["user_id"], email=user["email"], name=user["name"], role=user["role"],
                       tenant_id=claims["tid"], session_id=claims["sid"], created_at=utcnow())


# ---------- configuration ----------

@pytest.mark.parametrize(
    "raw, dsn, connect_args",
    [
        # Neon: the pooler host has a direct twin without "-pooler".
        ("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/db?sslmode=require",
         "postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/db", {"ssl": "require"}),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql://u:p@h:5432/db", {}),
        ("postgres://u:p@h/db?sslmode=disable", "postgresql://u:p@h/db", {}),
    ],
)
def test_direct_connection_dsn(raw, dsn, connect_args):
    assert direct_connection_dsn(raw) == (dsn, connect_args)


@pytest.mark.parametrize("raw", ["sqlite+aiosqlite://", "postgresql://u:p@pgbouncer.internal/db?pgbouncer=true"])
def test_direct_connection_dsn_refuses_what_cannot_listen(raw):
    with pytest.raises(ValueError):
        direct_connection_dsn(raw)


def test_transport_setting_is_validated(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_TRANSPORT", "websocket")
    with pytest.raises(RuntimeError, match="NOTIFICATION_TRANSPORT"):
        config.load_settings()
    monkeypatch.setenv("NOTIFICATION_TRANSPORT", "LISTEN")
    assert config.load_settings().notification_transport == "listen"
    monkeypatch.delenv("NOTIFICATION_TRANSPORT")
    assert config.load_settings().notification_transport == "poll"


def test_migration_trigger_matches_app_copy():
    spec = importlib.util.spec_from_file_location("migration_0016", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.NOTIFY_TRIGGER == rls.NOTIFY_TRIGGER_PG
    # Ids only: the payload never includes the title or the message.
    assert "title" not in rls.NOTIFY_TRIGGER_PG[0] and "message" not in rls.NOTIFY_TRIGGER_PG[0]


# ---------- fan-out (no database) ----------

async def test_fan_out_reaches_only_that_tenant_and_user():
    hub = NotificationHub("postgresql://unused/db")
    with hub.subscription("t1", "user_a") as mine, hub.subscription("t1", "user_a") as my_other_tab, \
            hub.subscription("t2", "user_a") as other_tenant, hub.subscription("t1", "user_b") as other_user:
        hub.dispatch("t1:user_a:notif_1")
        hub.dispatch("garbage")
        hub.dispatch("t1::notif_2")
        assert mine.get_nowait() == "notif_1" and my_other_tab.get_nowait() == "notif_1"
        assert mine.empty() and other_tenant.empty() and other_user.empty()
        assert hub.subscriber_count() == 4
    assert hub.subscriber_count() == 0


async def test_tenant_ids_containing_colons_still_route():
    hub = NotificationHub("postgresql://unused/db")
    with hub.subscription("org:east", "user_a") as queue:
        hub.dispatch("org:east:user_a:notif_1")
        assert queue.get_nowait() == "notif_1"


async def test_a_full_queue_collapses_to_one_resync_marker(monkeypatch):
    monkeypatch.setattr(hub_module, "QUEUE_SIZE", 3)
    hub = NotificationHub("postgresql://unused/db")
    with hub.subscription("t1", "u") as queue:
        for i in range(5):
            hub.dispatch(f"t1:u:notif_{i}")
        assert queue.qsize() == 2 and queue.get_nowait() is None  # resync, then what came after it
        assert queue.get_nowait() == "notif_4"


class FakeConnection:
    def __init__(self):
        self.listeners, self.on_close, self.terminated = {}, [], False

    def add_termination_listener(self, callback):
        self.on_close.append(callback)

    async def add_listener(self, channel, callback):
        self.listeners[channel] = callback

    async def execute(self, _sql):
        return "SELECT 1"

    def terminate(self):
        self.terminated = True

    def drop(self):
        for callback in self.on_close:
            callback(self)


async def test_hub_reconnects_with_backoff_and_resyncs_subscribers(monkeypatch):
    monkeypatch.setattr(hub_module, "RECONNECT_MIN_SECONDS", 0.01)
    connections: list[FakeConnection] = []
    attempts = 0

    async def connect(_dsn, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("database starting")
        connections.append(FakeConnection())
        return connections[-1]

    hub = NotificationHub("postgresql://h/db", connect=connect)
    with hub.subscription("t1", "u") as queue:
        assert await hub.wait_connected(2)
        connections[0].listeners["notifications"](connections[0], 1, "notifications", "t1:u:notif_1")
        assert await asyncio.wait_for(queue.get(), 1) == "notif_1"

        connections[0].drop()  # the server closed the connection
        assert await asyncio.wait_for(queue.get(), 2) is None  # reconnected: refetch what may have been missed
        assert len(connections) == 2 and connections[0].terminated and hub.connected
    await hub.stop()
    assert not hub.connected


# ---------- SQLite / poll fallback ----------

async def test_listen_falls_back_to_polling_without_postgres(client, session_factory, make_user, monkeypatch):
    listen = dataclasses.replace(hub_module.settings, notification_transport="listen",
                                 database_url="sqlite+aiosqlite://", database_direct_url=None)
    monkeypatch.setattr(hub_module, "settings", listen)
    assert hub_module.get_hub() is None

    _, headers = await make_user("nurse")
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    monkeypatch.setattr(notifications_router, "settings",
                        dataclasses.replace(notifications_router.settings, notification_stream_seconds=0.1))
    resp = await client.get("/api/notifications/stream", headers=headers)
    assert resp.status_code == 200
    assert [_frame(f)[0] for f in resp.text.split("\n\n") if f.strip()] == ["ready", "end"]


async def test_poll_transport_has_no_hub(monkeypatch):
    monkeypatch.setattr(hub_module, "settings", dataclasses.replace(hub_module.settings, notification_transport="poll"))
    assert hub_module.get_hub() is None


async def test_stream_polls_when_the_hub_cannot_connect(db, session_factory, make_user, monkeypatch):
    monkeypatch.setattr("app.notifications.stream.CONNECT_WAIT_SECONDS", 0.05)

    async def refuse(_dsn, **_kwargs):
        raise OSError("refused")

    hub = NotificationHub("postgresql://h/db", connect=refuse)
    user, headers = await make_user("nurse")
    events = notification_events(await _identity(user, headers), session_factory, duration=5, poll=0.05, hub=hub)
    assert _frame(await anext(events))[0] == "ready"
    mine = Notification(user_id=user["user_id"], title="t", message="m")
    db.add(mine)
    await db.commit()
    assert _frame(await anext(events)) == ("notification", f'{{"notification_id":"{mine.notification_id}"}}')
    await events.aclose()
    await hub.stop()


# ---------- Postgres: real LISTEN/NOTIFY ----------

@pytest.fixture
async def pg_hub():
    hub = NotificationHub(*direct_connection_dsn(TEST_DATABASE_URL))
    assert await hub.wait_connected(10)
    yield hub
    await hub.stop()


@requires_postgres
async def test_committed_insert_reaches_the_subscriber_and_a_rolled_back_one_does_not(
        db, session_factory, make_user, pg_hub):
    user, headers = await make_user("nurse")
    other, _ = await make_user("nurse")
    events = notification_events(await _identity(user, headers), session_factory, duration=10, poll=60, hub=pg_hub)
    assert _frame(await anext(events))[0] == "ready"
    next_frame = asyncio.ensure_future(anext(events))

    rolled_back = Notification(user_id=user["user_id"], title="Never committed", message="Patient Jane Doe")
    db.add(rolled_back)
    await db.flush()  # the trigger has fired inside the transaction...
    await asyncio.sleep(0.3)
    assert not next_frame.done()  # ...but nothing is delivered before commit
    await db.rollback()

    db.add(Notification(user_id=other["user_id"], title="Not yours", message="Other user"))
    await db.commit()
    mine = Notification(user_id=user["user_id"], title="Discharge Ready", message="Patient Jane Doe is ready")
    db.add(mine)
    await db.commit()

    # poll=60: nothing here polls, so the frame can only have come over LISTEN.
    frame = await asyncio.wait_for(next_frame, 5)
    assert _frame(frame) == ("notification", f'{{"notification_id":"{mine.notification_id}"}}')
    assert rolled_back.notification_id not in frame and "Jane" not in frame
    await events.aclose()


@requires_postgres
async def test_listen_isolates_tenants(db, session_factory, make_user, make_tenant, pg_hub):
    await make_tenant("north")
    south_user, south_headers = await make_user("nurse")
    north_user, _ = await make_user("nurse", tenant_id="north")
    with pg_hub.subscription("north", south_user["user_id"]) as wrong_tenant, \
            pg_hub.subscription("default", south_user["user_id"]) as right:
        db.add(Notification(user_id=south_user["user_id"], title="t", message="m"))
        await db.commit()
        delivered = await asyncio.wait_for(right.get(), 5)
        assert delivered.startswith("notif_") and wrong_tenant.empty()
    assert north_user["user_id"] != south_user["user_id"]


@requires_postgres
async def test_listen_stream_ends_when_the_session_is_revoked(db, session_factory, make_user, pg_hub):
    user, headers = await make_user("physician")
    identity = await _identity(user, headers)
    events = notification_events(identity, session_factory, duration=10, session_check=0.1, hub=pg_hub)
    await anext(events)
    await db.execute(update(AuthSession).where(AuthSession.session_id == identity.session_id)
                     .values(revoked_at=utcnow()))
    await db.commit()
    invalidate_identity(session_id=identity.session_id)
    assert _frame(await asyncio.wait_for(anext(events), 5)) == ("end", '{"reason":"session_inactive"}')
