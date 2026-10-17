"""SQLAlchemy engine, session factory and declarative base."""
import os
from collections.abc import AsyncIterator
from typing import Annotated
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends
from sqlalchemy import DateTime, MetaData, TypeDecorator, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

# Deterministic constraint names so Alembic migrations are stable across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that always round-trips as UTC (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict:
        """API representation: every column, datetimes as ISO-8601 strings (matches the old Mongo docs)."""
        out = {}
        for col in self.__table__.columns:
            value = getattr(self, col.key)
            out[col.key] = value.isoformat() if isinstance(value, datetime) else value
        return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_database_url(url: str) -> tuple[str, dict]:
    """Accept provider-style URLs (postgres://, ?sslmode=require) and adapt them for asyncpg."""
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = "require"
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, connect_args


def make_engine(url: str, **kwargs):
    url, connect_args = normalize_database_url(url)
    # Serverless platforms (Vercel) freeze processes between requests; pooled connections go stale.
    if os.environ.get("VERCEL") and "poolclass" not in kwargs:
        kwargs["poolclass"] = NullPool
    eng = create_async_engine(url, connect_args=connect_args, pool_pre_ping=True, **kwargs)
    if eng.dialect.name == "sqlite":
        # SQLite ignores foreign keys unless asked; enforce them so dev/test behave like Postgres.
        @event.listens_for(eng.sync_engine, "connect")
        def _enable_sqlite_fks(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return eng


engine = make_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit on success, roll back on any exception."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# scope="function": commit runs before the response is sent, so a failed commit surfaces as an error
# instead of a 200 the client believes. Every route takes `db: DB`.
DB = Annotated[AsyncSession, Depends(get_db, scope="function")]
