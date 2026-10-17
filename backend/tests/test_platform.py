"""Configuration, URL handling, seeding and migrations."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import load_settings
from app.core.db import normalize_database_url
from app.patients.models import Patient
from app.seed import DEMO_PATIENTS, seed_demo_data

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("missing", ["DATABASE_URL", "JWT_SECRET"])
def test_production_requires_explicit_secrets(monkeypatch, missing):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        load_settings()


def test_development_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("JWT_SECRET")
    s = load_settings()
    assert s.database_url.startswith("sqlite+aiosqlite:///") and len(s.jwt_secret) > 32
    assert not s.is_production


@pytest.mark.parametrize(
    "raw, url, connect_args",
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db", {}),
        ("postgresql://u:p@h/db?sslmode=require&channel_binding=require", "postgresql+asyncpg://u:p@h/db", {"ssl": "require"}),
        ("postgresql+asyncpg://u:p@h/db?sslmode=disable", "postgresql+asyncpg://u:p@h/db", {}),
        ("sqlite+aiosqlite:///./x.db", "sqlite+aiosqlite:///./x.db", {}),
    ],
)
def test_normalize_database_url(raw, url, connect_args):
    assert normalize_database_url(raw) == (url, connect_args)


@pytest.mark.anyio
async def test_seed_is_idempotent(db):
    assert await seed_demo_data(db) == len(DEMO_PATIENTS)
    await db.commit()
    assert await seed_demo_data(db) == 0
    assert await db.scalar(select(func.count()).select_from(Patient)) == len(DEMO_PATIENTS)


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "APP_ENV": "test", "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND_DIR, env=env, capture_output=True, text=True
    )


def test_migrations_match_models_and_downgrade(tmp_path):
    """`upgrade head` builds the schema, `check` finds no drift from the models, `downgrade base` reverses it."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    for args in (["upgrade", "head"], ["check"], ["downgrade", "base"], ["upgrade", "head"]):
        result = _alembic(*args, database_url=url)
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    _alembic("downgrade", "base", database_url=url)
