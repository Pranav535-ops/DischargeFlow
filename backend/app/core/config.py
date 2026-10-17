"""Runtime configuration, read once from the environment (and backend/.env)."""
import base64
import binascii
import hashlib
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
# Deterministic so a local SQLite database stays readable across restarts. Never used outside development/test.
_DEV_PHI_KEK = hashlib.sha256(b"dischargeflow-development-only-phi-key").digest()


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _decode_kek(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError("PHI_KEK must be base64-encoded")
    if len(key) != 32:
        raise RuntimeError("PHI_KEK must decode to exactly 32 bytes (AES-256)")
    return key


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    phi_kek: bytes
    jwt_secret: str | None
    cors_origins: list[str] = field(default_factory=list)
    default_tenant_id: str = "default"
    seed_on_startup: bool = False
    auto_migrate: bool = False
    emergent_llm_key: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env not in DEV_ENVS

    def require_jwt_secret(self) -> str:
        """The API needs a signing key; migrations and CLI commands do not, so this is checked at app creation."""
        if not self.jwt_secret:
            raise RuntimeError("JWT_SECRET must be set when APP_ENV is not development/test")
        return self.jwt_secret


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    is_dev = app_env in DEV_ENVS

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        if not is_dev:
            raise RuntimeError("DATABASE_URL must be set when APP_ENV is not development/test")
        database_url = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'dischargeflow.db').as_posix()}"

    raw_kek = os.environ.get("PHI_KEK", "").strip()
    if raw_kek:
        phi_kek = _decode_kek(raw_kek)
    elif is_dev:
        phi_kek = _DEV_PHI_KEK
    else:
        raise RuntimeError("PHI_KEK must be set when APP_ENV is not development/test")

    jwt_secret = os.environ.get("JWT_SECRET", "").strip() or None
    if not jwt_secret and is_dev:
        # Ephemeral per-process secret: dev sessions are invalidated on restart, which is fine locally.
        jwt_secret = secrets.token_urlsafe(48)
        logger.warning("JWT_SECRET not set; using an ephemeral development secret")

    return Settings(
        app_env=app_env,
        database_url=database_url,
        phi_kek=phi_kek,
        jwt_secret=jwt_secret,
        cors_origins=_split_csv(os.environ.get("CORS_ORIGINS", "http://localhost:3000")),
        default_tenant_id=os.environ.get("DEFAULT_TENANT_ID", "default").strip() or "default",
        seed_on_startup=_bool("SEED_ON_STARTUP"),
        auto_migrate=_bool("AUTO_MIGRATE"),
        emergent_llm_key=os.environ.get("EMERGENT_LLM_KEY") or None,
    )


settings = load_settings()
