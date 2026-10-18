"""Phase 4 operations: structured logs with PHI scrubbing, request ids, and PHI key (KEK) rotation."""
import json
import logging
import os

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import crypto
from app.core.observability import ContextFilter, JsonFormatter, PhiScrubFilter, request_id_var, scrub
from app.patients.models import Patient

pytestmark = pytest.mark.anyio


def _format(record: logging.LogRecord) -> dict:
    ContextFilter().filter(record)
    PhiScrubFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def test_scrubber_redacts_phi_shaped_text_but_keeps_timestamps():
    text = scrub("jane@x.org 555-201-3344 123-45-6789 MRN 4432 dob 1970-01-01 at 2026-10-18T10:03:16")
    assert text == "[EMAIL] [PHONE] [SSN] [MRN] dob [DATE] at 2026-10-18T10:03:16"


def test_json_log_lines_are_scrubbed_and_carry_context():
    token = request_id_var.set("req-12345678")
    try:
        record = logging.makeLogRecord({"name": "app.test", "levelno": logging.WARNING, "levelname": "WARNING",
                                        "msg": "lookup for %s failed", "args": ("jane@x.org",),
                                        "duration_ms": 12.5})
        out = _format(record)
    finally:
        request_id_var.reset(token)
    assert out["msg"] == "lookup for [EMAIL] failed" and out["request_id"] == "req-12345678"
    assert out["level"] == "WARNING" and out["duration_ms"] == 12.5 and out["ts"].endswith("Z")


def test_tracebacks_are_scrubbed():
    try:
        raise ValueError("bad DOB 02/03/1950 for jane@x.org")
    except ValueError:
        import sys

        record = logging.makeLogRecord({"msg": "boom", "levelname": "ERROR", "exc_info": sys.exc_info()})
    out = _format(record)
    assert "jane@x.org" not in out["exc"] and "02/03/1950" not in out["exc"] and "[EMAIL]" in out["exc"]


async def test_every_response_has_a_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="dischargeflow.access")
    resp = await client.get("/api/health")
    generated = resp.headers["x-request-id"]
    assert len(generated) == 32

    resp = await client.get("/api/patients", headers={"X-Request-ID": "alb-trace-abcdef12"})
    assert resp.status_code == 401 and resp.headers["x-request-id"] == "alb-trace-abcdef12"
    bad = await client.get("/api/health", headers={"X-Request-ID": "x"})  # too short: replaced, not trusted
    assert bad.headers["x-request-id"] != "x"

    lines = [r for r in caplog.records if r.name == "dischargeflow.access"]
    assert {(r.route, r.status) for r in lines} >= {("/api/health", 200), ("/api/patients", 401)}


async def test_kek_rotation_rewraps_every_dek(engine, db, monkeypatch):
    from app.core import migrate

    old_kek, new_kek = os.urandom(32), os.urandom(32)
    monkeypatch.setattr(migrate, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))

    def use_keys(current, previous=()):
        monkeypatch.setattr(crypto, "key_provider", lambda: crypto.RotatingKeyProvider(current, previous))
        monkeypatch.setattr(crypto, "_index_keys",
                            lambda purpose: tuple(crypto._index_key(k, purpose) for k in (current, *previous)))

    use_keys(old_kek)
    patient = Patient(name="Rota Tion", date_of_birth="1960-01-01", admission_date="2026-10-01", room_number="1",
                      diagnosis="Gout", attending_physician="Dr K")
    patient.set_mrn("default", "HOSP", "MRN-ROT-1")
    db.add(patient)
    await db.commit()
    old_wrapped, old_index, patient_id = patient.phi_dek, patient.mrn_index, patient.patient_id

    # The new key alone can't read rows wrapped by the old one...
    use_keys(new_kek)
    with pytest.raises(InvalidTag):
        crypto.key_provider().unwrap(old_wrapped)

    # ...so rotation runs with the old key listed as previous, re-wraps, and then the old key can go.
    use_keys(new_kek, (old_kek,))
    assert await migrate.rewrap_deks() == {"rewrapped": 1, "already_current": 0, "mrn_reindexed": 1}
    assert await migrate.rewrap_deks() == {"rewrapped": 0, "already_current": 1, "mrn_reindexed": 0}

    use_keys(new_kek)
    db.expire_all()
    rotated = await db.get(Patient, patient_id)
    assert rotated.phi_dek != old_wrapped and rotated.name == "Rota Tion"
    # The MRN blind index is keyed from the KEK too: it was recomputed, so lookups work without the old key.
    assert rotated.mrn_index != old_index
    assert rotated.mrn_index == crypto.blind_index("mrn", "default", "HOSP", "MRN-ROT-1")


def test_previous_keks_are_parsed_from_env(monkeypatch):
    import base64

    from app.core.config import load_settings

    keys = [base64.b64encode(os.urandom(32)).decode() for _ in range(2)]
    monkeypatch.setenv("PHI_KEK_PREVIOUS", ",".join(keys))
    assert [base64.b64encode(k).decode() for k in load_settings().phi_kek_previous] == keys
    monkeypatch.setenv("PHI_KEK_PREVIOUS", "not-base64!")
    with pytest.raises(RuntimeError):
        load_settings()


def test_database_url_from_rds_parts(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.delenv("DATABASE_URL")
    for k, v in {"DB_HOST": "db.internal", "DB_USER": "app", "DB_PASSWORD": "p@ss/word:1", "DB_NAME": "df"}.items():
        monkeypatch.setenv(k, v)
    assert load_settings().database_url == "postgresql://app:p%40ss%2Fword%3A1@db.internal:5432/df?sslmode=require"
