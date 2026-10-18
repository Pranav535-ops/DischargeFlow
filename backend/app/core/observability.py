"""Logs, request correlation and tracing (ADR-001 Phase 4, item 16).

- Structured JSON logs (LOG_FORMAT=json, the production default), one object per line, carrying the request id,
  tenant and user so CloudWatch Logs Insights can filter on them.
- A PHI scrubber on every handler. It is a backstop, not a licence: code must still never log patient fields,
  request bodies or prompts. It redacts the patterns that slip in by accident (emails, phone numbers, SSNs, MRNs,
  dates of birth and other full dates) from messages, arguments and tracebacks.
- An X-Request-ID on every response (taken from the load balancer when valid, else generated) and one access-log
  line per request with method, route template, status and duration.
- OpenTelemetry traces for FastAPI and SQLAlchemy when OTEL_EXPORTER_OTLP_ENDPOINT is set and the optional
  requirements-otel.txt is installed (the Docker image installs it; Vercel does not).
"""
import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.tenancy import current_tenant_id

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PHI_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)"), "[PHONE]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:MRN|mrn)[:#\s]*\w+"), "[MRN]"),
    # Full dates only: timestamps keep their time part ("2026-10-18T10:03:16" stays readable as a time).
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b(?!T)|\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "[DATE]"),
]
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def scrub(text: str) -> str:
    for pattern, replacement in PHI_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class PhiScrubFilter(logging.Filter):
    """Redacts PHI-shaped substrings. Formats the message first so arguments are scrubbed too."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not lose the log line
            message = str(record.msg)
        record.msg, record.args = scrub(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.tenant_id = current_tenant_id.get()
        record.user_id = user_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "tenant_id", "user_id"):
            if getattr(record, key, None):
                out[key] = getattr(record, key)
        for key, value in vars(record).items():  # structured fields passed with extra={...}
            if key not in _STD_ATTRS and key not in out and key not in ("request_id", "tenant_id", "user_id"):
                out[key] = value
        if record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, default=str)


JsonFormatter.converter = time.gmtime


def configure_logging(log_format: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.addFilter(PhiScrubFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's own access log duplicates ours and logs raw query strings; keep its errors only.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


access_logger = logging.getLogger("dischargeflow.access")


class RequestContextMiddleware:
    """Assigns the request id, echoes it as X-Request-ID, and writes one access-log line per request."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            access_logger.info(
                "%s %s %s", scope["method"], getattr(route, "path", "unmatched"), status,
                extra={"http_method": scope["method"], "route": getattr(route, "path", None), "status": status,
                       "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)


def setup_tracing(app, engine, service_name: str = "dischargeflow-api") -> bool:
    """Instrument FastAPI and SQLAlchemy when an OTLP endpoint is configured. Returns whether tracing is on."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logging.getLogger(__name__).warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but requirements-otel.txt is not installed")
        return False

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
        "deployment.environment": os.environ.get("APP_ENV", "development"),
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # No request/response bodies or query strings on spans: they can carry PHI. Statements are parameterised,
    # so SQL spans hold no values.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, exclude_spans=["receive", "send"])
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    return True
