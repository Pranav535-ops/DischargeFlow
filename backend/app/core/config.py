"""Runtime configuration, read once from the environment (and backend/.env)."""
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")

logger = logging.getLogger(__name__)

DEV_ENVS = {"development", "test"}


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    jwt_secret: str
    cors_origins: list[str] = field(default_factory=list)
    seed_on_startup: bool = False
    emergent_llm_key: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env not in DEV_ENVS


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    is_dev = app_env in DEV_ENVS

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        if not is_dev:
            raise RuntimeError("DATABASE_URL must be set when APP_ENV is not development/test")
        database_url = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'dischargeflow.db').as_posix()}"

    jwt_secret = os.environ.get("JWT_SECRET", "").strip()
    if not jwt_secret:
        if not is_dev:
            raise RuntimeError("JWT_SECRET must be set when APP_ENV is not development/test")
        # Ephemeral per-process secret: dev sessions are invalidated on restart, which is fine locally.
        jwt_secret = secrets.token_urlsafe(48)
        logger.warning("JWT_SECRET not set; using an ephemeral development secret")

    return Settings(
        app_env=app_env,
        database_url=database_url,
        jwt_secret=jwt_secret,
        cors_origins=_split_csv(os.environ.get("CORS_ORIGINS", "http://localhost:3000")),
        seed_on_startup=_bool("SEED_ON_STARTUP"),
        emergent_llm_key=os.environ.get("EMERGENT_LLM_KEY") or None,
    )


settings = load_settings()
