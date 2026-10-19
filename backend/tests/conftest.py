"""Test fixtures.

Runs against in-memory SQLite by default. Set TEST_DATABASE_URL (e.g. postgresql+asyncpg://...) to run the same
suite against Postgres, as CI does; tenant-isolation (RLS) tests only run there.
"""
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-use-0123456789")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core import rls
from app.core.db import get_db, make_engine
from app.core.security import create_access_token
from app.core.tenancy import current_tenant_id, reset_role_cache, use_tenant
from app.identity import permissions
from app.identity.models import AuthSession, Tenant, User
from app.identity.sessions import SESSION_TTL
from app.core.db import utcnow
from app.main import app
from app.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_TENANT = "default"

requires_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs TEST_DATABASE_URL (Postgres RLS)")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def engine():
    if TEST_DATABASE_URL:
        eng = make_engine(TEST_DATABASE_URL, poolclass=NullPool)
    else:
        # StaticPool: every session shares the single in-memory database connection.
        eng = make_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            for stmt in rls.drop_unit_scope_statements():
                await conn.execute(text(stmt))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        if eng.dialect.name == "postgresql":
            for stmt in rls.install_statements() + rls.APPEND_ONLY_TRIGGER_PG + rls.NOTIFY_TRIGGER_PG:
                await conn.execute(text(stmt))
        else:
            for stmt in rls.APPEND_ONLY_TRIGGER_SQLITE:
                await conn.execute(text(stmt))
    reset_role_cache()
    permissions.clear_cache()

    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(tenant_id=DEFAULT_TENANT, name="Default Hospital"))
        await session.flush()
        await permissions.seed_role_permissions(session)
        await session.commit()

    yield eng
    async with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            for stmt in rls.drop_unit_scope_statements():
                await conn.execute(text(stmt))
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(session_factory):
    """A session for arranging and asserting on data directly, outside the API (owner role: not subject to RLS)."""
    token = current_tenant_id.set(DEFAULT_TENANT)
    async with session_factory() as session:
        yield session
    current_tenant_id.reset(token)


@pytest.fixture
async def client(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def make_tenant(session_factory):
    async def _make(tenant_id: str):
        async with session_factory() as session:
            session.add(Tenant(tenant_id=tenant_id, name=f"Hospital {tenant_id}"))
            await session.commit()
        return tenant_id

    return _make


@pytest.fixture
def make_user(session_factory):
    """Create a persisted user with an active session; return (user_dict, auth_headers)."""

    async def _make(role: str = "nurse", name: str | None = None, tenant_id: str = DEFAULT_TENANT):
        token = current_tenant_id.set(tenant_id)
        try:
            async with session_factory() as session:
                await use_tenant(session, tenant_id)
                user = User(email=f"{role}.{os.urandom(4).hex()}@example.test", name=name or f"Test {role}", role=role)
                session.add(user)
                await session.flush()
                now = utcnow()
                auth_session = AuthSession(user_id=user.user_id, current_hash="0" * 64, created_at=now,
                                           last_used_at=now, expires_at=now + SESSION_TTL)
                session.add(auth_session)
                await session.commit()
                doc = user.to_dict()
                access = create_access_token(user.user_id, auth_session.session_id, tenant_id)
        finally:
            current_tenant_id.reset(token)
        return doc, {"Authorization": f"Bearer {access}"}

    return _make


PATIENT = {
    "name": "Jane Doe",
    "date_of_birth": "1970-01-01",
    "admission_date": "2026-10-01",
    "room_number": "12A",
    "diagnosis": "Community-acquired pneumonia",
    "attending_physician": "Dr. Test",
}


@pytest.fixture
def create_patient(client):
    async def _create(headers, **overrides):
        resp = await client.post("/api/patients", json={**PATIENT, **overrides}, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    return _create
