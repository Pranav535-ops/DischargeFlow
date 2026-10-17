import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from starlette.middleware.cors import CORSMiddleware

from app.ai.router import router as ai_router
from app.analytics.router import router as analytics_router
from app.audit.models import AuditLog
from app.audit.router import router as audit_router
from app.chatbot.router import router as chatbot_router
from app.core.config import settings
from app.core.db import DB, SessionLocal, engine, utcnow
from app.discharge.router import tasks_router, workflows_router
from app.identity.router import auth_router, users_router
from app.insurance.models import InsuranceClaim
from app.insurance.router import router as insurance_router
from app.notifications.router import router as notifications_router
from app.patients.models import Patient
from app.patients.router import router as patients_router
from app.seed import seed_demo_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

VERSION = "2.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.seed_on_startup:
        async with SessionLocal() as db:
            inserted = await seed_demo_data(db)
            await db.commit()
        if inserted:
            logger.info("Seeded %d demo patients on startup", inserted)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="DischargeFlow API", version=VERSION, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    api = APIRouter(prefix="/api")
    for router in (
        auth_router, users_router, patients_router, workflows_router, tasks_router, notifications_router,
        analytics_router, ai_router, insurance_router, chatbot_router, audit_router,
    ):
        api.include_router(router)

    @api.get("/health", tags=["Health"])
    async def health_check(db: DB):
        try:
            await db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Health check: database unreachable")
            return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "unreachable"})
        counts = {}
        for name, model in (("patients", Patient), ("insurance_claims", InsuranceClaim), ("audit_logs", AuditLog)):
            counts[name] = await db.scalar(select(func.count()).select_from(model))
        return {
            "status": "healthy",
            "version": VERSION,
            "database": engine.dialect.name,
            "timestamp": utcnow().isoformat(),
            "metrics": counts,
        }

    app.include_router(api)
    return app


app = create_app()
