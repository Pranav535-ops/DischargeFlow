"""Phase 2 security controls: refresh sessions, permissions, audit chain, PHI encryption, tenant isolation."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.audit.models import AuditLog
from app.core import crypto
from app.core.db import utcnow
from app.core.migrate import encrypt_legacy_phi
from app.identity import router as identity_router
from app.identity.models import AuthSession, RolePermission
from app.identity.sessions import REFRESH_COOKIE
from app.patients.models import Patient
from tests.conftest import requires_postgres

pytestmark = pytest.mark.anyio


@pytest.fixture
def oauth(monkeypatch):
    async def fake_profile(session_id: str) -> dict:
        return {"email": f"{session_id}@example.test", "name": f"User {session_id}", "picture": None}

    monkeypatch.setattr(identity_router, "fetch_oauth_profile", fake_profile)


async def sign_in(client, who: str = "alice") -> dict:
    resp = await client.post("/api/auth/session", json={"session_id": who})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- refresh sessions ----------

async def test_refresh_rotates_cookie_and_issues_new_access_token(client, oauth):
    first = await sign_in(client)
    old_cookie = client.cookies.get(REFRESH_COOKIE)

    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 200, resp.text
    new_cookie = client.cookies.get(REFRESH_COOKIE)
    assert new_cookie and new_cookie != old_cookie
    assert resp.json()["user"]["user_id"] == first["user"]["user_id"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {resp.json()['token']}"})
    assert me.status_code == 200


async def test_refresh_without_cookie_is_refused(client):
    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 401
    client.cookies.set(REFRESH_COOKIE, "garbage", path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_concurrent_refresh_within_grace_is_tolerated(client, oauth):
    await sign_in(client)
    stale = client.cookies.get(REFRESH_COOKIE)
    assert (await client.post("/api/auth/refresh")).status_code == 200
    # A second tab still holding the previous token refreshes moments later.
    client.cookies.set(REFRESH_COOKIE, stale, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 200


async def test_reused_refresh_token_after_grace_revokes_session(client, oauth, db):
    signed = await sign_in(client)
    stale = client.cookies.get(REFRESH_COOKIE)
    assert (await client.post("/api/auth/refresh")).status_code == 200

    # Age the rotation past the grace window, then replay the old token (as a thief would).
    session = await db.scalar(select(AuthSession).where(AuthSession.user_id == signed["user"]["user_id"]))
    session.rotated_at = utcnow() - timedelta(minutes=5)
    await db.commit()

    client.cookies.set(REFRESH_COOKIE, stale, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401

    await db.refresh(session)
    assert session.revoked_reason == "refresh_token_reuse"
    # The legitimate holder's access token stops working too.
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {signed['token']}"})).status_code == 401


async def test_logout_revokes_session_and_access_token(client, oauth):
    signed = await sign_in(client)
    headers = {"Authorization": f"Bearer {signed['token']}"}
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 401
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_expired_access_token_is_rejected(client, make_user, monkeypatch):
    from app.core import security

    monkeypatch.setattr(security, "ACCESS_TOKEN_TTL", timedelta(seconds=-1))
    user, _ = await make_user("nurse")
    token = security.create_access_token(user["user_id"], "ses_x", "default")
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401 and resp.json()["detail"] == "Session expired"


# ---------- permissions ----------

async def test_permissions_come_from_the_database(client, make_user, create_patient, db):
    from app.identity import permissions

    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    assert (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=nurse)).status_code == 403

    db.add(RolePermission(role="nurse", permission="workflow:manage"))
    await db.commit()
    permissions.clear_cache()
    assert (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=nurse)).status_code == 200


# ---------- audit chain ----------

async def test_audit_chain_verifies_and_is_append_only(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    pid = (await create_patient(admin))["patient_id"]
    for flag in (True, False, True):
        await client.post(f"/api/patients/{pid}/consent", json={"gdpr_consent": flag}, headers=admin)

    report = (await client.get("/api/audit/verify", headers=admin)).json()
    assert report == {"valid": True, "entries": 3, "broken_at_seq": None}
    logs = (await client.get("/api/audit/logs", headers=admin)).json()
    assert [entry["seq"] for entry in logs] == [3, 2, 1]
    assert logs[1]["prev_hash"] == logs[2]["hash"]

    with pytest.raises((DBAPIError, IntegrityError)):
        await db.execute(text("UPDATE audit_logs SET details = 'tampered'"))
        await db.commit()
    await db.rollback()
    with pytest.raises((DBAPIError, IntegrityError)):
        await db.execute(text("DELETE FROM audit_logs"))
        await db.commit()
    await db.rollback()


async def test_audit_verification_detects_tampering(client, make_user, create_patient, db):
    _, admin = await make_user("admin")
    pid = (await create_patient(admin))["patient_id"]
    for flag in (True, False):
        await client.post(f"/api/patients/{pid}/consent", json={"gdpr_consent": flag}, headers=admin)

    # Simulate someone with owner access bypassing the trigger.
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        await db.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_append_only"))
    else:
        await db.execute(text("DROP TRIGGER audit_logs_no_update"))
    await db.execute(text("UPDATE audit_logs SET details = 'rewritten history' WHERE seq = 1"))
    await db.commit()

    report = (await client.get("/api/audit/verify", headers=admin)).json()
    assert report["valid"] is False and report["broken_at_seq"] == 1


# ---------- PHI encryption ----------

async def test_patient_identifiers_are_encrypted_at_rest(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    created = await create_patient(nurse, name="Grace Hopper", diagnosis="Fractured wrist")
    assert created["name"] == "Grace Hopper" and "phi_dek" not in created

    raw = (await db.execute(
        text("SELECT name, date_of_birth, diagnosis, room_number FROM patients WHERE patient_id = :p"),
        {"p": created["patient_id"]},
    )).one()
    assert all(crypto.is_encrypted(v) for v in raw[:3])
    assert "Grace" not in raw.name and raw.room_number == "12A"

    listed = (await client.get("/api/patients", headers=nurse)).json()
    assert listed[0]["name"] == "Grace Hopper" and listed[0]["diagnosis"] == "Fractured wrist"


async def test_ciphertext_is_bound_to_its_row(make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    a = await create_patient(nurse, name="Patient A")
    b = await create_patient(nurse, name="Patient B")
    pa, pb = await db.get(Patient, a["patient_id"]), await db.get(Patient, b["patient_id"])
    pb._name, pb.phi_dek = pa._name, pa.phi_dek  # copy A's ciphertext and key onto B
    with pytest.raises(Exception):
        _ = pb.name  # associated data (patient id) no longer matches


async def test_erasure_destroys_data_key(client, make_user, create_patient, db):
    _, nurse = await make_user("nurse")
    _, admin = await make_user("admin")
    pid = (await create_patient(nurse))["patient_id"]
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200

    row = (await db.execute(text("SELECT phi_dek, name FROM patients WHERE patient_id = :p"), {"p": pid})).one()
    assert row.phi_dek is None and row.name == "[REDACTED]"
    assert (await client.get(f"/api/patients/{pid}", headers=nurse)).json()["name"] == "[REDACTED]"


async def test_legacy_plaintext_rows_are_backfilled(engine, db, monkeypatch):
    from app.core import migrate

    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(migrate, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    await db.execute(text(
        "INSERT INTO patients (patient_id, tenant_id, name, date_of_birth, diagnosis, admission_date, room_number, "
        "attending_physician, status, gdpr_erased, created_at, updated_at) VALUES "
        "('pat_legacy00001', 'default', 'Old Plain', '1950-01-01', 'Gout', '2026-01-01', '9', 'Dr Q', 'admitted', "
        "false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ))
    await db.commit()

    assert await encrypt_legacy_phi() == 1
    assert await encrypt_legacy_phi() == 0  # idempotent

    raw = (await db.execute(text("SELECT name FROM patients WHERE patient_id = 'pat_legacy00001'"))).scalar_one()
    assert crypto.is_encrypted(raw)
    db.expire_all()
    assert (await db.get(Patient, "pat_legacy00001")).name == "Old Plain"


# ---------- tenant isolation (Postgres row-level security) ----------

@requires_postgres
async def test_tenants_cannot_see_each_others_data(client, make_user, make_tenant, create_patient):
    await make_tenant("hospital_b")
    _, nurse_a = await make_user("nurse")
    _, nurse_b = await make_user("nurse", tenant_id="hospital_b")
    pid_a = (await create_patient(nurse_a, name="Only In A"))["patient_id"]

    assert [p["patient_id"] for p in (await client.get("/api/patients", headers=nurse_a)).json()] == [pid_a]
    assert (await client.get("/api/patients", headers=nurse_b)).json() == []
    assert (await client.get(f"/api/patients/{pid_a}", headers=nurse_b)).status_code == 404
    assert (await client.put(f"/api/patients/{pid_a}", json={"room_number": "X"}, headers=nurse_b)).status_code == 404

    pid_b = (await create_patient(nurse_b, name="Only In B"))["patient_id"]
    assert pid_b not in [p["patient_id"] for p in (await client.get("/api/patients", headers=nurse_a)).json()]


@requires_postgres
async def test_rls_rejects_writes_into_another_tenant(session_factory, make_tenant):
    from app.core.tenancy import current_tenant_id, use_tenant

    await make_tenant("hospital_b")
    token = current_tenant_id.set("default")
    try:
        async with session_factory() as session:
            await use_tenant(session, "default")
            session.add(Patient(tenant_id="hospital_b", name="Smuggled", date_of_birth="x", diagnosis="x",
                                admission_date="2026-01-01", room_number="1", attending_physician="x"))
            with pytest.raises(DBAPIError):
                await session.flush()
    finally:
        current_tenant_id.reset(token)


@requires_postgres
async def test_audit_log_is_insert_only_for_app_role(session_factory):
    from app.core.tenancy import current_tenant_id, use_tenant

    token = current_tenant_id.set("default")
    try:
        async with session_factory() as session:
            await use_tenant(session, "default")
            with pytest.raises(DBAPIError, match="permission denied"):
                await session.execute(text("TRUNCATE audit_logs"))
    finally:
        current_tenant_id.reset(token)
