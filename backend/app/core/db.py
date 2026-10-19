"""SQLAlchemy engine, session factory and declarative base."""
import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Annotated
from datetime import date, datetime, timezone
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

    # Columns never returned by the API (key material, token hashes).
    __api_exclude__: frozenset[str] = frozenset()

    def to_dict(self) -> dict:
        """API representation: every column, datetimes as ISO-8601 strings (matches the old Mongo docs)."""
        out = {}
        for col in self.__table__.columns:
            if col.name in self.__api_exclude__:
                continue
            # col.name, not col.key: encrypted columns map to private attributes behind public synonyms.
            value = getattr(self, col.name)
            out[col.name] = value.isoformat() if isinstance(value, (datetime, date)) else value
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


def uses_transaction_pooler(url: str) -> bool:
    """Transaction-mode poolers (Neon's "-pooler" hosts, ?pgbouncer=true).

    PgBouncer >= 1.21 handles asyncpg's prepared statements, but older or differently configured poolers break
    them, so pooled URLs get the conservative settings below.
    """
    parts = urlsplit(url)
    return "-pooler." in (parts.hostname or "") or dict(parse_qsl(parts.query)).get("pgbouncer") == "true"


def direct_connection_dsn(url: str) -> tuple[str, dict]:
    """A plain asyncpg DSN (and connect kwargs) for a session-level feature such as LISTEN.

    A transaction-mode pooler hands each transaction to any server connection, so a LISTEN through it never hears
    anything. Neon's "-pooler" hosts have a direct twin without the suffix; any other pooled URL needs
    DATABASE_DIRECT_URL. Raises ValueError for SQLite or a pooled URL with no known direct host.
    """
    pooled = uses_transaction_pooler(url)
    url, connect_args = normalize_database_url(url)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("LISTEN needs Postgres")
    parts = urlsplit("postgresql://" + url[len("postgresql+asyncpg://"):])
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "pgbouncer"]
    if pooled:
        host = parts.hostname or ""
        if "-pooler." not in host:
            raise ValueError("the database URL goes through a transaction pooler; set DATABASE_DIRECT_URL")
        parts = parts._replace(netloc=parts.netloc.replace(host, host.replace("-pooler.", ".", 1)))
    return urlunsplit(parts._replace(query=urlencode(query))), connect_args


def make_engine(url: str, **kwargs):
    pooler = uses_transaction_pooler(url)
    url, connect_args = normalize_database_url(url)
    if pooler:
        url = urlunsplit(urlsplit(url)._replace(
            query=urlencode({k: v for k, v in parse_qsl(urlsplit(url).query) if k != "pgbouncer"})))
        # SQLAlchemy's documented PgBouncer settings: no statement cache, unique statement names.
        connect_args |= {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    # Serverless platforms (Vercel) freeze processes between requests; pooled connections go stale.
    if (os.environ.get("VERCEL") or pooler) and "poolclass" not in kwargs:
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


# SQLite ignores `SELECT ... FOR UPDATE`, so two concurrent requests could both read the audit chain head and append
# the same sequence number (every PHI read appends one). Local SQLite is a single-process development database, so its
# request transactions simply run one at a time. Postgres relies on the row lock instead.
_sqlite_request_lock = asyncio.Lock() if engine.dialect.name == "sqlite" else None


async def get_db() -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit on success, roll back on any exception."""
    async with _sqlite_request_lock or contextlib.nullcontext():
        async with SessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """For code that outlives the request transaction (streaming responses open short sessions of their own)."""
    return SessionLocal


# scope="function": commit runs before the response is sent, so a failed commit surfaces as an error
# instead of a 200 the client believes. Every route takes `db: DB`.
DB = Annotated[AsyncSession, Depends(get_db, scope="function")]
