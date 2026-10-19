"""Announce new notifications with pg_notify

ADR-002 Phase 9, item 21 (finding 9, design change 4). An AFTER INSERT trigger on `notifications` calls
`pg_notify('notifications', '<tenant_id>:<user_id>:<notification_id>')`. Postgres delivers it only when the inserting
transaction commits. With NOTIFICATION_TRANSPORT=listen, each process's LISTEN hub hands the id to that user's open
streams; with the default `poll` nobody listens and the notify is a no-op. Ids only: no notification text goes out.

Postgres only (SQLite has no NOTIFY; it keeps polling). Additive, so the previous release is unaffected.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19 23:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of app.core.rls.NOTIFY_TRIGGER_PG at this revision.
NOTIFY_TRIGGER = [
    """CREATE OR REPLACE FUNCTION notifications_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('notifications', NEW.tenant_id || ':' || NEW.user_id || ':' || NEW.notification_id);
    RETURN NULL;
END $$""",
    "DROP TRIGGER IF EXISTS notifications_notify ON notifications",
    "CREATE TRIGGER notifications_notify AFTER INSERT ON notifications "
    "FOR EACH ROW EXECUTE FUNCTION notifications_notify()",
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in NOTIFY_TRIGGER:
            op.execute(stmt)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS notifications_notify ON notifications")
        op.execute("DROP FUNCTION IF EXISTS notifications_notify()")
