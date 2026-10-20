"""Operational commands:
`python -m app.cli {migrate,encrypt-phi,rewrap-deks,seed,summary-deadlines,escalate,sso-provider}`."""
import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.ai import lifecycle
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.migrate import encrypt_legacy_phi, migrate_all, rewrap_deks
from app.escalations.service import run_all_tenants
from app.core.tenancy import current_tenant_id, use_tenant
from app.identity.models import IdentityProvider, Tenant
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
    sub.add_parser("summary-deadlines",
                   help="Notify physicians of discharge summaries unsigned past SUMMARY_SIGN_HOURS (run on a schedule)")
    sub.add_parser("escalate", help="Escalate overdue tasks in every tenant (idempotent; run every few minutes)")
    sso = sub.add_parser("sso-provider", help="Register, list or disable a hospital's OIDC identity provider")
    sso_sub = sso.add_subparsers(dest="sso_command", required=True)
    add = sso_sub.add_parser("add", help="Register a provider for a tenant (e.g. from the Cognito Terraform outputs)")
    add.add_argument("--tenant", required=True, help="Tenant (hospital) id people sign in to")
    add.add_argument("--name", required=True, help="Shown on the sign-in button")
    add.add_argument("--issuer", required=True)
    add.add_argument("--client-id", required=True)
    add.add_argument("--authorize-url", required=True)
    add.add_argument("--token-url", required=True)
    add.add_argument("--jwks-url", help="Default: <issuer>/.well-known/jwks.json")
    add.add_argument("--scopes", default="openid email profile")
    add.add_argument("--groups-claim", default="groups", help="ID-token claim listing directory groups")
    add.add_argument("--no-require-mfa", action="store_true",
                     help="Accept tokens whose amr claim names no second factor (only if the IdP cannot send it)")
    sso_sub.add_parser("list", help="List providers")
    disable = sso_sub.add_parser("disable", help="Stop offering a provider (sign-in through it is refused)")
    disable.add_argument("provider_id")
    args = parser.parse_args()

    if args.command == "migrate":
        return asyncio.run(_run(migrate_all()))
    if args.command == "encrypt-phi":
        return asyncio.run(_run(_encrypt()))
    if args.command == "rewrap-deks":
        return asyncio.run(_run(rewrap_deks()))
    if args.command == "seed":
        return asyncio.run(_run(_seed(args.force)))
    if args.command == "summary-deadlines":
        return asyncio.run(_run(_summary_deadlines()))
    if args.command == "escalate":
        return asyncio.run(_run(run_all_tenants(SessionLocal)))
    if args.command == "sso-provider":
        return asyncio.run(_run(_sso_provider(args)))
    return 2


async def _encrypt() -> dict:
    return {"phi_rows_encrypted": await encrypt_legacy_phi()}


async def _summary_deadlines() -> dict:
    """One sweep over every tenant. Idempotent, so a scheduler may run it as often as it likes (e.g. hourly)."""
    async with SessionLocal() as db:
        tenant_ids = list(await db.scalars(select(Tenant.tenant_id)))
    notified = {}
    for tenant_id in tenant_ids:
        token = current_tenant_id.set(tenant_id)
        try:
            async with SessionLocal() as db:
                await use_tenant(db, tenant_id)
                notified[tenant_id] = await lifecycle.notify_overdue(db)
                await db.commit()
        finally:
            current_tenant_id.reset(token)
    return {"notified": notified}


if __name__ == "__main__":
    sys.exit(main())


async def _sso_provider(args) -> dict | int:
    """Identity providers are shared configuration (the app role may only read them), so operators manage them
    here, with the owner connection, rather than tenant admins over the API."""
    async with SessionLocal() as db:
        if args.sso_command == "list":
            rows = await db.scalars(select(IdentityProvider).order_by(IdentityProvider.created_at))
            return {"providers": [r.to_dict() for r in rows]}
        if args.sso_command == "disable":
            row = await db.get(IdentityProvider, args.provider_id)
            if row is None:
                print(f"No identity provider {args.provider_id}", file=sys.stderr)
                return 1
            row.active = False
            await db.commit()
            return {"disabled": row.provider_id}
        if await db.get(Tenant, args.tenant) is None:
            print(f"No tenant {args.tenant}", file=sys.stderr)
            return 1
        for url in (args.issuer, args.authorize_url, args.token_url, args.jwks_url):
            if url and not url.startswith("https://") and settings.is_production:
                print(f"Provider URLs must be https: {url}", file=sys.stderr)
                return 1
        row = IdentityProvider(
            tenant_id=args.tenant, name=args.name, issuer=args.issuer.rstrip("/"), client_id=args.client_id,
            authorize_url=args.authorize_url, token_url=args.token_url, jwks_url=args.jwks_url, scopes=args.scopes,
            groups_claim=args.groups_claim, require_mfa=not args.no_require_mfa, active=True,
        )
        db.add(row)
        await db.commit()
        return {"provider_id": row.provider_id, "tenant_id": row.tenant_id}
