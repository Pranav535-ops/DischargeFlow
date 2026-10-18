"""Live notification stream (SSE): per-user events, ids only, ends on revocation or timeout."""
import dataclasses

import pytest
from sqlalchemy import update

from app.core.db import get_session_factory, utcnow
from app.core.security import CurrentUser, decode_access_token, invalidate_identity
from app.identity.models import AuthSession
from app.main import app
from app.notifications import router as notifications_router
from app.notifications.models import Notification
from app.notifications.stream import notification_events

pytestmark = pytest.mark.anyio


def _frame(raw: str) -> tuple[str, str]:
    lines = dict(line.split(": ", 1) for line in raw.strip().splitlines() if not line.startswith(":"))
    return lines.get("event", ""), lines.get("data", "")


async def _identity(user: dict, headers: dict) -> CurrentUser:
    claims = decode_access_token(headers["Authorization"][7:])
    return CurrentUser(user_id=user["user_id"], email=user["email"], name=user["name"], role=user["role"],
                       tenant_id=claims["tid"], session_id=claims["sid"], created_at=utcnow())


async def test_stream_yields_only_own_new_notifications_without_text(db, session_factory, make_user):
    user, headers = await make_user("nurse")
    other, _ = await make_user("nurse")
    identity = await _identity(user, headers)
    events = notification_events(identity, session_factory, duration=5, poll=0.05)

    assert _frame(await anext(events))[0] == "ready"
    db.add(Notification(user_id=other["user_id"], title="Not yours", message="Other user"))
    mine = Notification(user_id=user["user_id"], title="Discharge Ready", message="Patient Jane Doe is ready")
    db.add(mine)
    await db.commit()

    frame = await anext(events)
    event, data = _frame(frame)
    assert event == "notification"
    assert mine.notification_id in data and "Jane" not in frame and "Discharge Ready" not in frame
    await events.aclose()


async def test_stream_ends_when_the_session_is_revoked(db, session_factory, make_user):
    user, headers = await make_user("physician")
    identity = await _identity(user, headers)
    events = notification_events(identity, session_factory, duration=5, poll=0.05)
    await anext(events)

    await db.execute(update(AuthSession).where(AuthSession.session_id == identity.session_id)
                     .values(revoked_at=utcnow()))
    await db.commit()
    invalidate_identity(session_id=identity.session_id)
    assert _frame(await anext(events)) == ("end", '{"reason":"session_inactive"}')


async def test_stream_endpoint_requires_auth_and_times_out(client, session_factory, make_user, monkeypatch):
    assert (await client.get("/api/notifications/stream")).status_code == 401

    _, headers = await make_user("pharmacist")
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    monkeypatch.setattr(notifications_router, "settings",
                        dataclasses.replace(notifications_router.settings, notification_stream_seconds=0.1))
    resp = await client.get("/api/notifications/stream", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"
    frames = [_frame(f)[0] for f in resp.text.split("\n\n") if f.strip()]
    assert frames == ["ready", "end"]
