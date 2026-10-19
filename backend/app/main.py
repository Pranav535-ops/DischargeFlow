import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.cors import CORSMiddleware

from app.ai import service as ai_service
from app.ai.router import router as ai_router
from app.analytics.router import router as analytics_router
from app.audit.router import router as audit_router
from app.chatbot.router import router as chatbot_router
from app.core.config import VERSION, settings
from app.core.db import DB, SessionLocal, engine, utcnow
from app.core.migrate import ensure_migrated
from app.core.observability import RequestContextMiddleware, configure_logging, setup_tracing
from app.discharge.router import plans_router, tasks_router, templates_router, workflows_router
from app.downtime.router import census_router
from app.downtime.router import router as downtime_router
from app.downtime.service import READ_ONLY_HEADER, enforce_read_only
from app.escalations.router import router as escalations_router
from app.identity.router import auth_router, integrations_router, units_router, users_router
from app.identity.scim_router import router as scim_router
from app.identity.sso_router import identity_providers_router, sso_auth_router
from app.insurance.router import router as insurance_router
from app.interop.hl7_router import router as hl7_router
from app.interop import smart
from app.interop.router import router as fhir_router
from app.interop.smart_router import router as smart_router
from app.notifications.hub import stop_hub
from app.notifications.router import router as notifications_router
from app.patients.router import access_router
from app.patients.router import router as patients_router
from app.seed import seed_demo_data

configure_logging(settings.log_format, settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.seed_on_startup:
        async with SessionLocal() as db:
            inserted = await seed_demo_data(db)
            await db.commit()
        if inserted:
            logger.info("Seeded %d demo patients on startup", inserted)
    yield
    await stop_hub()
    await engine.dispose()


def create_app() -> FastAPI:
    settings.require_jwt_secret()  # fail fast: the API cannot issue or verify tokens without it

    app = FastAPI(title="DischargeFlow API", version=VERSION, lifespan=lifespan)
    # Summaries use the EHR's medication, problem and allergy lists when the physician launched from the EHR
    # (ADR-002 item 15). Registered here because nothing may import interop.
    ai_service.set_clinical_context_source(smart.summary_context)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[READ_ONLY_HEADER],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        return response

    if settings.auto_migrate:
        @app.middleware("http")
        async def migrate_on_first_request(request: Request, call_next):
            try:
                await ensure_migrated()
            except Exception:
                logger.exception("Automatic migration failed")
                return JSONResponse(status_code=503, content={"detail": "Database migration in progress or failed"})
            return await call_next(request)

    # Read-only (downtime) mode refuses every write route before it runs (ADR-002 item 6).
    api = APIRouter(prefix="/api", dependencies=[Depends(enforce_read_only)])
    for router in (
        auth_router, users_router, patients_router, workflows_router, tasks_router, notifications_router,
        analytics_router, ai_router, insurance_router, chatbot_router, audit_router, fhir_router,
        integrations_router, hl7_router, units_router, access_router, downtime_router, census_router,
        escalations_router, smart_router,
        plans_router, templates_router,
        sso_auth_router, identity_providers_router, scim_router,
    ):
        api.include_router(router)

    @api.get("/health", tags=["Health"])
    async def health_check(db: DB):
        # Unauthenticated, so it reports reachability only; record counts would leak tenant data.
        try:
            await db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Health check: database unreachable")
            return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "unreachable"})
        return {
            "status": "healthy",
            "version": VERSION,
            "database": engine.dialect.name,
            "timestamp": utcnow().isoformat(),
        }

    app.include_router(api)
    # Added last so it is outermost: every response, including errors from other middleware, gets a request id.
    app.add_middleware(RequestContextMiddleware)
    setup_tracing(app, engine)
    return app


app = create_app()
