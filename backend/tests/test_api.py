"""Auth, RBAC, patients, insurance, chatbot, notifications, audit and health."""
import jwt
import pytest

from app.core.config import settings

pytestmark = pytest.mark.anyio


# ---------- auth / RBAC ----------

async def test_requests_without_token_are_rejected(client):
    assert (await client.get("/api/patients")).status_code == 401


async def test_forged_token_is_rejected(client):
    forged = jwt.encode({"user_id": "x", "email": "x@x", "name": "x", "role": "admin"}, "wrong-secret-but-long-enough-for-hs256-0123456789", algorithm="HS256")
    resp = await client.get("/api/users", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_cookie_auth_and_me(client, make_user):
    user, headers = await make_user("pharmacist", name="Pat Pharm")
    token = headers["Authorization"].split(" ", 1)[1]
    client.cookies.set("session_token", token)
    me = (await client.get("/api/auth/me")).json()
    client.cookies.clear()
    assert me["user_id"] == user["user_id"] and me["role"] == "pharmacist"


async def test_role_gates(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    _, pharmacist = await make_user("pharmacist")
    _, admin = await make_user("admin")
    patient = await create_patient(nurse)

    assert (await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=nurse)).status_code == 403
    assert (await client.post("/api/patients", json={}, headers=pharmacist)).status_code == 403
    assert (await client.get("/api/users", headers=nurse)).status_code == 403
    assert (await client.get("/api/audit/logs", headers=nurse)).status_code == 403
    assert (await client.delete(f"/api/patients/{patient['patient_id']}/data", headers=nurse)).status_code == 403
    assert (await client.get("/api/users", headers=admin)).status_code == 200


async def test_admin_updates_role(client, make_user):
    target, _ = await make_user("nurse")
    _, admin = await make_user("admin")
    resp = await client.put(f"/api/users/{target['user_id']}/role", json={"role": "physician"}, headers=admin)
    assert resp.json() == {"success": True, "role": "physician"}
    users = {u["user_id"]: u for u in (await client.get("/api/users", headers=admin)).json()}
    assert users[target["user_id"]]["role"] == "physician"

    assert (await client.put(f"/api/users/{target['user_id']}/role", json={"role": "ceo"}, headers=admin)).status_code == 400
    assert (await client.put("/api/users/user_nope/role", json={"role": "nurse"}, headers=admin)).status_code == 404


async def test_session_exchange_creates_user_once(client, monkeypatch):
    from app.identity import router as identity_router

    async def fake_profile(session_id: str) -> dict:
        return {"email": "new.user@example.test", "name": "New User", "picture": None}

    monkeypatch.setattr(identity_router, "fetch_oauth_profile", fake_profile)
    first = await client.post("/api/auth/session", json={"session_id": "abc"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["user"]["role"] == "nurse"
    assert "session_token" in first.headers.get("set-cookie", "")

    second = (await client.post("/api/auth/session", json={"session_id": "def"})).json()
    assert second["user"]["user_id"] == body["user"]["user_id"]

    notes = (await client.get("/api/notifications", headers={"Authorization": f"Bearer {body['token']}"})).json()
    assert [n["title"] for n in notes] == ["Welcome to DischargeFlow"]
    assert (await client.post("/api/auth/session", json={})).status_code == 400


# ---------- patients ----------

async def test_patient_crud_and_update_whitelist(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    patient = await create_patient(nurse)
    pid = patient["patient_id"]
    assert pid.startswith("pat_") and patient["status"] == "admitted"
    assert isinstance(patient["created_at"], str)

    resp = await client.put(f"/api/patients/{pid}", headers=nurse,
                            json={"room_number": "99Z", "patient_id": "hijack", "gdpr_erased": True})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["room_number"] == "99Z"
    assert updated["patient_id"] == pid and updated["gdpr_erased"] is False

    assert (await client.put(f"/api/patients/{pid}", headers=nurse, json={"status": "teleported"})).status_code == 422
    assert (await client.get("/api/patients/pat_missing", headers=nurse)).status_code == 404
    assert len((await client.get("/api/patients?status=admitted", headers=nurse)).json()) == 1
    assert (await client.get("/api/patients?status=discharged", headers=nurse)).json() == []


async def test_consent_and_erasure_are_audited(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    _, admin = await make_user("admin")
    pid = (await create_patient(nurse))["patient_id"]

    assert (await client.post(f"/api/patients/{pid}/consent", json={"gdpr_consent": True}, headers=nurse)).status_code == 200
    assert (await client.delete(f"/api/patients/{pid}/data", headers=admin)).status_code == 200

    erased = (await client.get(f"/api/patients/{pid}", headers=nurse)).json()
    assert erased["name"] == "[REDACTED]" and erased["gdpr_erased"] is True and erased["gdpr_consent"] is True

    trail = (await client.get(f"/api/audit/patients/{pid}", headers=admin)).json()
    assert {e["action"] for e in trail} == {"UPDATE_CONSENT", "GDPR_ERASURE"}
    assert all(e["ip_address"] for e in trail)


# ---------- insurance ----------

async def test_claims_lifecycle_and_reports(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    _, admin = await make_user("admin")
    pid = (await create_patient(nurse))["patient_id"]

    async def new_claim(insurer, billed):
        resp = await client.post("/api/insurance/claims", headers=nurse, json={
            "patient_id": pid, "insurer_name": insurer, "policy_number": "P-1",
            "diagnosis_codes": ["J18.9"], "procedure_codes": ["99223"], "billed_amount": billed,
        })
        assert resp.status_code == 200, resp.text
        return resp.json()["claim_id"]

    c1 = await new_claim("Acme Health", 1000.10)
    c2 = await new_claim("Acme Health", 500.20)
    await new_claim("Beta Care", 250.00)

    resp = await client.put(f"/api/insurance/claims/{c1}/status", headers=nurse,
                            json={"status": "paid", "allowed_amount": 900.0, "paid_amount": 850.55})
    assert resp.status_code == 200
    await client.put(f"/api/insurance/claims/{c2}/status", headers=nurse, json={"status": "denied", "denial_reason": "No auth"})
    assert (await client.put(f"/api/insurance/claims/{c2}/status", headers=nurse, json={"status": "lost"})).status_code == 422

    claim = (await client.get(f"/api/insurance/claims/{c1}", headers=nurse)).json()
    assert claim["paid_amount"] == 850.55 and claim["diagnosis_codes"] == ["J18.9"]

    report = (await client.get("/api/insurance/reports", headers=nurse)).json()
    assert report["total_claims"] == 3
    assert report["total_billed"] == 1750.30
    assert report["total_paid"] == 850.55
    assert report["denial_rate"] == 33.3
    assert report["accepted_count"] == 1 and report["pending_count"] == 1
    assert {r["insurer"]: r["count"] for r in report["by_insurer"]} == {"Acme Health": 2, "Beta Care": 1}

    assert len((await client.get("/api/insurance/claims?status=denied", headers=nurse)).json()) == 1
    assert (await client.post("/api/insurance/claims", headers=nurse, json={
        "patient_id": "pat_missing", "insurer_name": "X", "policy_number": "", "billed_amount": 1,
    })).status_code == 404

    actions = [e["action"] for e in (await client.get("/api/audit/logs?resource_type=insurance_claim", headers=admin)).json()]
    assert actions.count("CREATE_CLAIM") == 3 and actions.count("UPDATE_CLAIM_STATUS") == 2


# ---------- chatbot ----------

async def test_chatbot_intents(client, make_user):
    _, nurse = await make_user("nurse")
    _, admin = await make_user("admin")

    async def say(message):
        resp = await client.post("/api/chatbot/message", json={"message": message}, headers=nurse)
        assert resp.status_code == 200, resp.text
        return resp.json()

    hint = await say("create patient")
    assert hint["intent"] == "create_patient" and "structured format" in hint["response"]

    made = await say("Create patient\nPatient Name: Ada Lovelace\nRoom: 7C\nDiagnosis: Fracture\nPhysician: Dr. Who")
    assert made["data"]["patients"][0]["room_number"] == "7C"

    task = await say("create a task for patient 1 to give meds")
    assert task["intent"] == "create_task" and task["data"]["tasks"][0]["title"] == "Give meds"
    assert task["data"]["tasks"][0]["priority"] == 2

    claim = await say("Submit claim\nPatient: Ada\nInsurer Name: Acme\nPolicy Number: 42\nBilled Amount: $1,200.50\nICD: S52.5, S52.6")
    assert claim["intent"] == "submit_claim"
    assert claim["data"]["claims"][0]["billed_amount"] == 1200.5
    assert claim["data"]["claims"][0]["diagnosis_codes"] == ["S52.5", "S52.6"]

    assert (await say("list patients"))["data"]["count"] == 1
    assert len((await say("my tasks"))["data"]["tasks"]) == 1
    assert "1 insurance claims" in (await say("show stats"))["response"]
    assert (await say("any billing updates?"))["intent"] == "list_claims"
    assert (await say("help"))["intent"] == "help"
    assert (await say("sing me a song"))["intent"] == "unknown"

    chatbot_logs = (await client.get("/api/audit/logs?resource_type=chatbot&limit=100", headers=admin)).json()
    assert len(chatbot_logs) == 10


async def test_chatbot_claim_without_patients(client, make_user):
    _, nurse = await make_user("nurse")
    resp = await client.post("/api/chatbot/message", headers=nurse,
                             json={"message": "Submit claim\nInsurer Name: Acme\nBilled Amount: 10"})
    assert resp.json()["data"] is None and "no patients" in resp.json()["response"]


# ---------- notifications / health ----------

async def test_notifications_mark_read(client, make_user, create_patient):
    _, physician = await make_user("physician")
    _, nurse = await make_user("nurse")
    pid = (await create_patient(nurse))["patient_id"]
    await client.post(f"/api/workflows/initiate/{pid}", headers=physician)

    notes = (await client.get("/api/notifications", headers=nurse)).json()
    assert len(notes) == 1 and notes[0]["read"] is False
    await client.put(f"/api/notifications/{notes[0]['notification_id']}/read", headers=physician)  # not theirs
    assert (await client.get("/api/notifications", headers=nurse)).json()[0]["read"] is False
    await client.put("/api/notifications/read-all", headers=nurse)
    assert (await client.get("/api/notifications", headers=nurse)).json()[0]["read"] is True


async def test_health_and_security_headers(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy" and body["metrics"] == {"patients": 0, "insurance_claims": 0, "audit_logs": 0}
    assert resp.headers["X-Frame-Options"] == "DENY"


async def test_seed_endpoint_is_gone(client):
    assert (await client.post("/api/seed")).status_code in (404, 405)


def test_settings_secret_is_from_env():
    assert settings.jwt_secret == "test-secret-not-for-production-use-0123456789"
