"""Configuration, URL handling, seeding and migrations."""
import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.audit.service import verify_chain
from app.core import crypto, rls
from app.core.config import load_settings
from app.core.db import make_engine, normalize_database_url
from app.patients.models import Patient
from app.seed import DEMO_PATIENTS, seed_demo_data

BACKEND_DIR = Path(__file__).resolve().parents[1]


VALID_KEK = base64.b64encode(b"k" * 32).decode()


def _production_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    monkeypatch.setenv("PHI_KEK", VALID_KEK)


@pytest.mark.parametrize("missing", ["DATABASE_URL", "PHI_KEK"])
def test_production_requires_explicit_secrets(monkeypatch, missing):
    _production_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        load_settings()


def test_production_api_requires_jwt_secret_but_migrations_do_not(monkeypatch):
    _production_env(monkeypatch)
    monkeypatch.delenv("JWT_SECRET")
    s = load_settings()  # migrations and CLI commands load settings without a signing key
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        s.require_jwt_secret()


@pytest.mark.parametrize("bad", ["not-base64!!", base64.b64encode(b"short").decode()])
def test_phi_kek_must_be_32_bytes_of_base64(monkeypatch, bad):
    _production_env(monkeypatch)
    monkeypatch.setenv("PHI_KEK", bad)
    with pytest.raises(RuntimeError, match="PHI_KEK"):
        load_settings()


def test_development_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("JWT_SECRET")
    monkeypatch.delenv("PHI_KEK", raising=False)
    s = load_settings()
    assert s.database_url.startswith("sqlite+aiosqlite:///") and len(s.jwt_secret) > 32
    assert len(s.phi_kek) == 32 and not s.is_production


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


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/db?sslmode=require", True),
        ("postgresql://u:p@db.example.com/db?pgbouncer=true", True),
        ("postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/db?sslmode=require", False),
    ],
)
def test_transaction_pooler_detection(raw, expected):
    from sqlalchemy.pool import NullPool

    from app.core.db import make_engine, uses_transaction_pooler

    assert uses_transaction_pooler(raw) is expected
    eng = make_engine(raw)  # no connection is opened here
    assert "pgbouncer" not in str(eng.url)
    assert isinstance(eng.pool, NullPool) is expected


@pytest.mark.anyio
async def test_seed_is_idempotent(db):
    assert await seed_demo_data(db) == len(DEMO_PATIENTS)
    await db.commit()
    assert await seed_demo_data(db) == 0
    assert await db.scalar(select(func.count()).select_from(Patient)) == len(DEMO_PATIENTS)


def test_server_imports_by_path_from_repo_root():
    """Vercel loads backend/server.py by file path with the repo root as cwd; `app` must still resolve."""
    repo_root = BACKEND_DIR.parent
    code = "import runpy; ns = runpy.run_path('backend/server.py'); print(type(ns['app']).__name__)"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", code], cwd=repo_root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FastAPI"


def _run(module_args: list[str], database_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "APP_ENV": "test", "DATABASE_URL": database_url}
    env.pop("PHI_KEK", None)  # use the test/dev key, same as the in-process app
    return subprocess.run([sys.executable, "-m", *module_args], cwd=BACKEND_DIR, env=env, capture_output=True,
                          text=True)


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    return _run(["alembic", *args], database_url)


async def _reset_database(url: str) -> None:
    """Start from an empty schema (the shared Postgres test database may hold tables from other tests)."""
    if not url.startswith("postgresql"):
        return
    eng = make_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await eng.dispose()


@pytest.fixture
def migration_url(tmp_path):
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'migrate.db').as_posix()}"


@pytest.mark.anyio
async def test_migrations_match_models_and_downgrade(migration_url):
    """`upgrade head` builds the schema, `check` finds no drift from the models, `downgrade base` reverses it."""
    await _reset_database(migration_url)
    for args in (["upgrade", "head"], ["check"], ["downgrade", "base"], ["upgrade", "head"]):
        result = _alembic(*args, database_url=migration_url)
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"

    if migration_url.startswith("postgresql"):
        eng = make_engine(migration_url, poolclass=NullPool)
        async with eng.connect() as conn:
            policies = set(await conn.scalars(text("SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'")))
            triggers = set(await conn.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")))
        await eng.dispose()
        assert policies == set(rls.TENANT_TABLES)  # migration and app agree on what is tenant-scoped
        assert "audit_logs_append_only" in triggers
    _alembic("downgrade", "base", database_url=migration_url)


@pytest.mark.anyio
async def test_upgrade_from_phase1_data_encrypts_and_chains(migration_url):
    """Real 0001-era data: plaintext patients and unchained audit rows survive `app.cli migrate` intact."""
    await _reset_database(migration_url)
    assert _alembic("upgrade", "0001", database_url=migration_url).returncode == 0

    eng = make_engine(migration_url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(
            "INSERT INTO patients (patient_id, name, date_of_birth, admission_date, room_number, diagnosis, "
            "attending_physician, status, gdpr_erased, created_at, updated_at) VALUES ('pat_phase1aaaaa', "
            "'Legacy Lee', '1940-02-02', '2026-01-01', '3', 'Asthma', 'Dr P', 'admitted', false, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        for i in range(3):
            await conn.execute(text(
                "INSERT INTO audit_logs (log_id, user_id, user_name, action, resource_type, timestamp) "
                f"VALUES ('log_phase1_{i}', 'u1', 'Old User', 'UPDATE_CONSENT', 'patient', CURRENT_TIMESTAMP)"
            ))

    result = _run(["app.cli", "migrate"], migration_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"phi_rows_encrypted": 1' in result.stdout

    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        raw_name = await session.scalar(text("SELECT name FROM patients WHERE patient_id = 'pat_phase1aaaaa'"))
        assert crypto.is_encrypted(raw_name)
        assert (await session.get(Patient, "pat_phase1aaaaa")).name == "Legacy Lee"
        assert await session.scalar(text("SELECT tenant_id FROM patients")) == "default"
        report = await verify_chain(session, "default")
        assert report == {"valid": True, "entries": 3, "broken_at_seq": None}
    await eng.dispose()
    _alembic("downgrade", "base", database_url=migration_url)
