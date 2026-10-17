"""Test fixtures.

Runs against in-memory SQLite by default. Set TEST_DATABASE_URL (e.g. postgresql+asyncpg://...) to run the same
suite against Postgres, as CI does.
"""
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-use-0123456789")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core.db import get_db, make_engine
from app.core.security import create_session_token
from app.identity.models import User
from app.main import app
from app.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


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
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(session_factory):
    """A session for arranging and asserting on data directly, outside the API."""
    async with session_factory() as session:
        yield session


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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(session_factory):
    """Create a persisted user with a role and return (user_dict, auth_headers)."""

    async def _make(role: str = "nurse", name: str | None = None):
        async with session_factory() as session:
            user = User(email=f"{role}.{os.urandom(4).hex()}@example.test", name=name or f"Test {role}", role=role)
            session.add(user)
            await session.commit()
            doc = user.to_dict()
        return doc, {"Authorization": f"Bearer {create_session_token(doc)}"}

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
