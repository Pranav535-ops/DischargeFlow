"""Schema migrations plus the PHI encryption backfill, runnable from the CLI or automatically on first request.

Automatic migration (AUTO_MIGRATE=true) exists for serverless hosts like Vercel, which have no release step and
give every preview deployment its own fresh database branch. A Postgres advisory lock makes concurrent cold starts
wait for one migrator. On ECS/Kubernetes the migration runs as a one-off task instead (ADR-001).
"""
import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app.core.config import BACKEND_DIR
from app.core.db import SessionLocal, engine
from app.core.tenancy import current_tenant_id, use_tenant

logger = logging.getLogger(__name__)

_LOCK_KEY = 74_219_001  # arbitrary, constant advisory-lock id for "dischargeflow migrations"
_migrated = False
_lock = asyncio.Lock()


def alembic_config() -> Config:
    cfg = Config(str(Path(BACKEND_DIR) / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(BACKEND_DIR) / "alembic"))
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def current_revision() -> str | None:
    async with engine.connect() as conn:
        try:
            return await conn.scalar(text("SELECT version_num FROM alembic_version"))
        except ProgrammingError:
            return None
        except Exception:
            # SQLite raises OperationalError for a missing table.
            return None


async def upgrade_schema() -> None:
    # Alembic's env.py runs its own event loop, so it needs a thread of its own.
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")


async def encrypt_legacy_phi() -> int:
    """Encrypt patient identifiers still stored as plaintext. Idempotent; returns how many rows changed."""
    from app.identity.models import Tenant
    from app.patients.models import Patient

    changed = 0
    async with SessionLocal() as db:
        tenant_ids = list(await db.scalars(select(Tenant.tenant_id)))
    for tenant_id in tenant_ids:
        token = current_tenant_id.set(tenant_id)
        try:
            async with SessionLocal() as db:
                await use_tenant(db, tenant_id)
                for patient in await db.scalars(select(Patient).where(Patient.tenant_id == tenant_id)):
                    if patient.needs_encryption:
                        patient.encrypt_legacy_fields()
                        changed += 1
                await db.commit()
        finally:
            current_tenant_id.reset(token)
    return changed


async def migrate_all() -> dict:
    await upgrade_schema()
    encrypted = await encrypt_legacy_phi()
    return {"revision": await current_revision(), "phi_rows_encrypted": encrypted}


async def ensure_migrated() -> None:
    """Bring the database to head once per process. Safe under concurrent cold starts."""
    global _migrated
    if _migrated:
        return
    async with _lock:
        if _migrated:
            return
        head = head_revision()
        if await current_revision() != head:
            if engine.dialect.name == "postgresql":
                async with engine.connect() as lock_conn:
                    await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
                    try:
                        if await current_revision() != head:
                            logger.info("Auto-migrating database to %s", head)
                            result = await migrate_all()
                            logger.info("Auto-migration finished: %s", result)
                    finally:
                        await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            else:
                await migrate_all()
        _migrated = True
