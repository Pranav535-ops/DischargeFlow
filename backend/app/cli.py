"""Operational commands: `python -m app.cli seed`."""
import argparse
import asyncio
import sys

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.seed import seed_demo_data


async def _seed(force: bool) -> int:
    if settings.is_production and not force:
        print("Refusing to seed demo data with APP_ENV=production (pass --force to override).", file=sys.stderr)
        return 1
    async with SessionLocal() as db:
        inserted = await seed_demo_data(db)
        await db.commit()
    await engine.dispose()
    print(f"Seeded {inserted} demo patients." if inserted else "Database already has patients; nothing seeded.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed", help="Insert demo patients into an empty database")
    seed.add_argument("--force", action="store_true", help="Allow seeding when APP_ENV=production")
    args = parser.parse_args()

    if args.command == "seed":
        return asyncio.run(_seed(args.force))
    return 2


if __name__ == "__main__":
    sys.exit(main())
