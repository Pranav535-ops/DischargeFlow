"""Operational commands: `python -m app.cli {migrate,encrypt-phi,rewrap-deks,seed,escalate}`."""
import argparse
import asyncio
import json
import sys

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.migrate import encrypt_legacy_phi, migrate_all, rewrap_deks
from app.escalations.service import run_all_tenants
from app.seed import seed_demo_data


async def _seed(force: bool) -> int:
    if settings.is_production and not force:
        print("Refusing to seed demo data with APP_ENV=production (pass --force to override).", file=sys.stderr)
        return 1
    async with SessionLocal() as db:
        inserted = await seed_demo_data(db)
        await db.commit()
    print(f"Seeded {inserted} demo patients." if inserted else "Database already has patients; nothing seeded.")
    return 0


async def _run(coro) -> int:
    try:
        result = await coro
        if isinstance(result, int):
            return result
        print(json.dumps(result))
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head, then encrypt any plaintext patient identifiers")
    sub.add_parser("encrypt-phi", help="Encrypt patient identifiers still stored as plaintext")
    sub.add_parser("rewrap-deks", help="Re-wrap patient data keys with the current PHI_KEK (KEK rotation)")
    seed = sub.add_parser("seed", help="Insert demo patients into an empty database")
    seed.add_argument("--force", action="store_true", help="Allow seeding when APP_ENV=production")
    sub.add_parser("escalate", help="Escalate overdue tasks in every tenant (idempotent; run every few minutes)")
    args = parser.parse_args()

    if args.command == "migrate":
        return asyncio.run(_run(migrate_all()))
    if args.command == "encrypt-phi":
        return asyncio.run(_run(_encrypt()))
    if args.command == "rewrap-deks":
        return asyncio.run(_run(rewrap_deks()))
    if args.command == "seed":
        return asyncio.run(_run(_seed(args.force)))
    if args.command == "escalate":
        return asyncio.run(_run(run_all_tenants(SessionLocal)))
    return 2


async def _encrypt() -> dict:
    return {"phi_rows_encrypted": await encrypt_legacy_phi()}


if __name__ == "__main__":
    sys.exit(main())
