"""Runtime configuration, read once from the environment (and backend/.env)."""
import base64
import binascii
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")

logger = logging.getLogger(__name__)

VERSION = "2.2.0"  # API version, reported by /api/health, OpenAPI and the FHIR CapabilityStatement

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
    # Retired KEKs, still accepted for unwrapping until `app.cli rewrap-deks` has moved every row to phi_kek.
    phi_kek_previous: tuple[bytes, ...] = ()
    default_tenant_id: str = "default"
    seed_on_startup: bool = False
    auto_migrate: bool = False
    # AI gateway (app/ai/llm.py). "mock" is deterministic and offline; "none" disables AI summaries.
    llm_provider: str = "none"
    anthropic_api_key: str | None = None
    aws_region: str | None = None
    summary_model: str = "claude-sonnet-5-5"
    assistant_model: str = "claude-haiku-5-5"
    log_format: str = "text"  # "json" in production: one object per line for CloudWatch
    log_level: str = "INFO"
    # Development/test only: POST /api/auth/dev-login signs in as any role without OAuth (Playwright E2E).
    enable_dev_login: bool = False
    # Who may request an account by signing in with Google. A new sign-in from an allowed domain (any domain when the
    # list is empty) gets the no-access role `pending` until an admin approves it; others are refused. With
    # self_registration off, only people an admin invited (POST /api/users) can sign in. Invited users are always let
    # in, whatever their domain.
    allowed_email_domains: tuple[str, ...] = ()
    self_registration: bool = True
    # ADR-002 item 3: staff see only patients on their own units (plus unassigned patients and break-glass grants),
    # enforced by RLS on Postgres. Off by default, so units can be set up before anyone loses access.
    unit_scoped_access: bool = False
    # ADR-002 item 6: refuse every write (503) on every instance, whatever the per-tenant downtime switch says. For
    # outages where the database must not be written at all (restore, failover). The normal switch is an admin's
    # POST /api/downtime/start, stored in the database so that every serverless instance sees it.
    read_only_mode: bool = False
    # How long one notification stream (GET /api/notifications/stream) stays open before the client reconnects.
    # Kept under the serverless function limit on Vercel; a long-running host (ECS) can raise it.
    notification_stream_seconds: float = 25.0
    # Shared-workstation safety (ADR-002 item 4). The browser locks after idle_lock_minutes without input; the server
    # refuses to refresh a session unused for session_idle_minutes, which catches tabs closed before they locked.
    idle_lock_minutes: float = 5.0
    session_idle_minutes: float = 20.0
    # ADR-002 item 2 (code seam; needs the hospital's identity provider, e.g. Cognito federated with its directory).
    # When oidc_issuer and oidc_client_id are set, POST /api/auth/oidc accepts an ID token signed by a key from
    # oidc_jwks_url (default: the issuer's /.well-known/jwks.json, Cognito's layout). oidc_require_mfa refuses tokens
    # whose `amr` claim does not show a second factor.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_jwks_url: str | None = None
    oidc_require_mfa: bool = False
    # ADR-002 item 5 (code seam; needs AWS under a BAA). "kms" wraps new patient data keys with the KMS key
    # phi_kms_key_id; PHI_KEK stays the key for blind indexes and for unwrapping data keys wrapped before the switch,
    # until `app.cli rewrap-deks` has moved them all to KMS.
    phi_key_provider: str = "local"
    phi_kms_key_id: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env not in DEV_ENVS

    def require_jwt_secret(self) -> str:
        """The API needs a signing key; migrations and CLI commands do not, so this is checked at app creation."""
        if not self.jwt_secret:
            raise RuntimeError("JWT_SECRET must be set when APP_ENV is not development/test")
        return self.jwt_secret


def _database_url_from_parts() -> str:
    """postgres URL from DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD.

    On AWS the password lives in the RDS-managed Secrets Manager secret and ECS injects it as DB_PASSWORD, so no
    full connection string (with the password inside) has to be stored anywhere.
    """
    host = os.environ.get("DB_HOST", "").strip()
    if not host:
        return ""
    user = quote(os.environ.get("DB_USER", "postgres"), safe="")
    password = quote(os.environ.get("DB_PASSWORD", ""), safe="")
    port = os.environ.get("DB_PORT", "5432").strip() or "5432"
    name = os.environ.get("DB_NAME", "dischargeflow").strip() or "dischargeflow"
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode=require"


def _minutes(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} must be a number of minutes")
    if not minimum <= value <= 24 * 60:
        raise RuntimeError(f"{name} must be between {minimum:g} and 1440 minutes")
    return value


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    is_dev = app_env in DEV_ENVS

    database_url = os.environ.get("DATABASE_URL", "").strip() or _database_url_from_parts()
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

    phi_kek_previous = tuple(_decode_kek(k) for k in _split_csv(os.environ.get("PHI_KEK_PREVIOUS", "")))

    phi_key_provider = os.environ.get("PHI_KEY_PROVIDER", "").strip().lower() or "local"
    phi_kms_key_id = os.environ.get("PHI_KMS_KEY_ID", "").strip() or None
    if phi_key_provider not in ("local", "kms"):
        raise RuntimeError("PHI_KEY_PROVIDER must be 'local' or 'kms'")
    if phi_key_provider == "kms" and not phi_kms_key_id:
        raise RuntimeError("PHI_KMS_KEY_ID must be set when PHI_KEY_PROVIDER is 'kms'")

    # The issuer is compared with the token's `iss` claim exactly, so a trailing slash would refuse every token.
    oidc_issuer = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/") or None

    jwt_secret = os.environ.get("JWT_SECRET", "").strip() or None
    if not jwt_secret and is_dev:
        # Ephemeral per-process secret: dev sessions are invalidated on restart, which is fine locally.
        jwt_secret = secrets.token_urlsafe(48)
        logger.warning("JWT_SECRET not set; using an ephemeral development secret")

    return Settings(
        app_env=app_env,
        database_url=database_url,
        phi_kek=phi_kek,
        phi_kek_previous=phi_kek_previous,
        jwt_secret=jwt_secret,
        cors_origins=_split_csv(os.environ.get("CORS_ORIGINS", "http://localhost:3000")),
        default_tenant_id=os.environ.get("DEFAULT_TENANT_ID", "default").strip() or "default",
        seed_on_startup=_bool("SEED_ON_STARTUP"),
        auto_migrate=_bool("AUTO_MIGRATE"),
        # Development and tests default to the offline mock; production must choose a provider explicitly.
        llm_provider=os.environ.get("LLM_PROVIDER", "").strip().lower() or ("mock" if is_dev else "none"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        aws_region=os.environ.get("AWS_REGION", "").strip() or None,
        summary_model=os.environ.get("LLM_SUMMARY_MODEL", "").strip() or "claude-sonnet-5-5",
        assistant_model=os.environ.get("LLM_ASSISTANT_MODEL", "").strip() or "claude-haiku-5-5",
        log_format=os.environ.get("LOG_FORMAT", "").strip().lower() or ("text" if is_dev else "json"),
        log_level=os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO",
        # Never honoured outside development/test, whatever the variable says.
        enable_dev_login=is_dev and _bool("ENABLE_DEV_LOGIN"),
        allowed_email_domains=tuple(d.lower().lstrip("@") for d in _split_csv(os.environ.get("ALLOWED_EMAIL_DOMAINS", ""))),
        self_registration=_bool("SELF_REGISTRATION", True),
        unit_scoped_access=_bool("UNIT_SCOPED_ACCESS"),
        read_only_mode=_bool("READ_ONLY_MODE"),
        notification_stream_seconds=float(os.environ.get("NOTIFICATION_STREAM_SECONDS", "").strip() or 25),
        idle_lock_minutes=_minutes("IDLE_LOCK_MINUTES", 5, 1),
        # An open tab refreshes its 15-minute access token about a minute before expiry, so anything under 16 minutes
        # would sign out people who are working.
        session_idle_minutes=_minutes("SESSION_IDLE_MINUTES", 20, 16),
        oidc_issuer=oidc_issuer,
        oidc_client_id=os.environ.get("OIDC_CLIENT_ID", "").strip() or None,
        oidc_jwks_url=os.environ.get("OIDC_JWKS_URL", "").strip() or None,
        oidc_require_mfa=_bool("OIDC_REQUIRE_MFA"),
        phi_key_provider=phi_key_provider,
        phi_kms_key_id=phi_kms_key_id,
    )


settings = load_settings()
