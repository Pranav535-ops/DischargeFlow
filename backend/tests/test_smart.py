"""ADR-002 items 14 and 15: SMART on FHIR EHR launch, and read-only clinical context from the EHR.

The EHR (authorization server, JWKS and FHIR server) is a fake behind httpx.MockTransport: no network is used. Not
tested against a real EHR sandbox.
"""
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.ai import service as ai_service
from app.ai.grounding import check_grounding
from app.ai.schemas import DischargeSummaryDoc
from app.audit.models import AuditLog
from app.interop import fhir_client, smart
from app.interop.models import EhrUserLink, SmartLaunch
from app.patients.models import Encounter, Patient

pytestmark = pytest.mark.anyio

ISS = "https://ehr.example/fhir"
CLIENT_ID = "dischargeflow-app"
REDIRECT = "https://dischargeflow.example/launch"
VERIFIER = "v" * 43 + "erifier-0123456789"
EHR_TOKEN = "ehr-access-token-SECRET-123"


class FakeEhr:
    """Authorization server + FHIR server of an EHR, enough for an EHR launch."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "ehr-key-1"
        self.codes: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.practitioner_email: str | None = None
        self.id_claims: dict = {}
        self.signing_key = self.key
        self.revoked = False
        self.patient_identifiers = [{"system": "urn:oid:1.2.3.mrn", "value": "MRN-777"}]

    # -- the browser's trip to the authorization endpoint, which redirects back with a code --
    def authorize(self, authorize_url: str) -> tuple[str, dict]:
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = query
        return code, query

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {"iss": ISS, "aud": CLIENT_ID, "sub": "prac-1", "iat": now, "exp": now + 300, "nonce": nonce,
                  "fhirUser": "Practitioner/prac-1", **self.id_claims}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": self.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/fhir/.well-known/smart-configuration":
            return httpx.Response(200, json={
                "issuer": ISS, "jwks_uri": "https://ehr.example/jwks",
                "authorization_endpoint": "https://ehr.example/authorize", "token_endpoint": "https://ehr.example/token",
                "code_challenge_methods_supported": ["S256"], "capabilities": ["launch-ehr", "sso-openid-connect"]})
        if path == "/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            issued = self.codes.pop(form.get("code", ""), None)
            if (issued is None or form.get("grant_type") != "authorization_code"
                    or form.get("redirect_uri") != issued["redirect_uri"] or form.get("client_id") != CLIENT_ID
                    or smart.s256(form.get("code_verifier", "")) != issued["code_challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": EHR_TOKEN, "token_type": "Bearer", "expires_in": 600, "scope": issued["scope"],
                "id_token": self.id_token(issued["nonce"]), "patient": "ehr-pat-1", "encounter": "ehr-enc-1"})
        if path.startswith("/fhir/"):
            if request.headers.get("Authorization") != f"Bearer {EHR_TOKEN}" or self.revoked:
                return httpx.Response(401, json={"resourceType": "OperationOutcome"})
            return self.fhir(path[len("/fhir/"):], dict(request.url.params))
        return httpx.Response(404)

    def fhir(self, path: str, params: dict) -> httpx.Response:
        if path == "Practitioner/prac-1":
            telecom = [{"system": "email", "value": self.practitioner_email}] if self.practitioner_email else []
            return httpx.Response(200, json={"resourceType": "Practitioner", "id": "prac-1",
                                             "name": [{"given": ["Ada"], "family": "Lovelace"}], "telecom": telecom})
        if path == "Patient/ehr-pat-1":
            return httpx.Response(200, json={"resourceType": "Patient", "id": "ehr-pat-1",
                                             "identifier": self.patient_identifiers})
        if path == "Encounter/ehr-enc-1":
            return httpx.Response(200, json={"resourceType": "Encounter", "id": "ehr-enc-1",
                                             "identifier": [{"system": "urn:visit", "value": "V-100"}]})
        assert params.get("patient") == "ehr-pat-1"
        entries = {
            "MedicationRequest": [
                {"resourceType": "MedicationRequest", "status": "active",
                 "medicationCodeableConcept": {"text": "Lisinopril 10 MG Oral Tablet"},
                 "dosageInstruction": [{"text": "10 mg by mouth once daily"}]},
                {"resourceType": "MedicationRequest", "status": "entered-in-error",
                 "medicationCodeableConcept": {"text": "Warfarin 5 MG"}},
                {"resourceType": "MedicationRequest", "status": "active",
                 "medicationReference": {"display": "Metformin 500 MG Oral Tablet"},
                 "dosageInstruction": [{"text": "500 mg twice daily with meals"}]},
            ],
            "Condition": [
                {"resourceType": "Condition", "code": {"coding": [
                    {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9",
                     "display": "Type 2 diabetes mellitus"},
                    {"system": "http://snomed.info/sct", "code": "44054006"}]},
                 "clinicalStatus": {"coding": [{"code": "active"}]},
                 "category": [{"coding": [{"code": "problem-list-item"}]}]},
                {"resourceType": "Condition", "code": {"text": "Chronic systolic heart failure", "coding": [
                    {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I50.22"}]}},
                {"resourceType": "Condition", "code": {"text": "Hypertension", "coding": [
                    {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I10"}]}},
                # Refuted: neither listed nor proposed.
                {"resourceType": "Condition", "code": {"text": "Dementia", "coding": [
                    {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "F03.90"}]},
                 "verificationStatus": {"coding": [{"code": "refuted"}]}},
            ],
            "AllergyIntolerance": [
                {"resourceType": "AllergyIntolerance", "code": {"text": "Penicillin"}, "criticality": "high",
                 "reaction": [{"manifestation": [{"text": "Hives"}]}]},
            ],
        }[path]
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset",
                                         "entry": [{"resource": r} for r in entries]})

    def fhir_calls(self) -> int:
        return sum(r.url.path.startswith("/fhir/") and ".well-known" not in r.url.path for r in self.requests)


@pytest.fixture
def ehr(monkeypatch):
    fake = FakeEhr()
    monkeypatch.setattr(fhir_client, "http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    smart._jwks_cache.clear()
    fhir_client.forget()
    yield fake
    fhir_client.forget()


@pytest.fixture
async def setup(client, make_user, create_patient, db, ehr):
    """An admin who registered the EHR, a physician the Practitioner's email matches, and a patient with the MRN
    and visit number the EHR knows."""
    admin_user, admin = await make_user("admin")
    physician_user, physician = await make_user("physician")
    resp = await client.post("/api/smart/issuers", headers=admin, json={
        "iss": ISS + "/", "name": "Test EHR", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "mrn_system": "urn:oid:1.2.3.mrn", "mrn_authority": "hosp", "visit_system": "urn:visit"})
    assert resp.status_code == 200, resp.text
    issuer = resp.json()
    assert issuer["iss"] == ISS
    patient = await create_patient(physician)
    row = await db.get(Patient, patient["patient_id"])
    row.set_mrn("default", "HOSP", "MRN-777")
    encounter = await db.scalar(select(Encounter).where(Encounter.patient_id == row.patient_id))
    encounter.visit_number = "V-100"
    await db.commit()
    ehr.practitioner_email = physician_user["email"].upper()
    return {"admin": admin, "physician": physician, "physician_user": physician_user, "issuer": issuer,
            "patient": patient, "encounter_id": encounter.encounter_id}


async def do_launch(client, ehr, verifier=VERIFIER, tamper=None):
    resp = await client.post("/api/smart/launch", json={"iss": ISS, "launch": "opaque-launch-1",
                                                       "code_challenge": smart.s256(VERIFIER)})
    assert resp.status_code == 200, resp.text
    started = resp.json()
    code, query = ehr.authorize(started["authorize_url"])
    assert query["state"] == started["state"]
    body = {"state": started["state"], "code": code, "code_verifier": verifier}
    if tamper:
        tamper(body)
    return await client.post("/api/smart/token", json=body), query


async def audit_rows(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)))


# ---------- item 14: launch ----------

async def test_unlisted_iss_is_refused_before_any_request(client, ehr):
    resp = await client.post("/api/smart/launch", json={"iss": "https://evil.example/fhir", "launch": "x",
                                                       "code_challenge": smart.s256(VERIFIER)})
    assert resp.status_code == 403
    assert ehr.requests == []


async def test_ehr_launch_signs_in_and_maps_patient_context(client, setup, ehr, db):
    resp, query = await do_launch(client, ehr)
    assert resp.status_code == 200, resp.text
    # The authorization request: SMART scopes, PKCE S256, aud = iss, our registered client and redirect.
    assert query["scope"] == "launch openid fhirUser patient/*.read"
    assert query["code_challenge_method"] == "S256" and query["aud"] == ISS and query["launch"] == "opaque-launch-1"
    assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == REDIRECT and query["response_type"] == "code"

    body = resp.json()
    assert body["user"]["user_id"] == setup["physician_user"]["user_id"] and body["token"]
    assert "df_refresh" in resp.headers.get("set-cookie", "")
    assert body["launch"]["matched"] is True
    assert body["launch"]["patient_id"] == setup["patient"]["patient_id"]
    assert body["launch"]["encounter_id"] == setup["encounter_id"]
    # The EHR's token never reaches the browser, and is stored only as ciphertext.
    assert EHR_TOKEN not in resp.text and "ehr-pat-1" not in resp.text
    launch = await db.scalar(select(SmartLaunch))
    assert launch.status == "active" and launch.access_token.startswith("enc:") and EHR_TOKEN not in launch.access_token
    assert "ehr-pat-1" not in (launch.ehr_patient or "") and "access_token" not in launch.to_dict()
    # The EHR user is linked for next time, and the launch is audited (ids only).
    link = await db.scalar(select(EhrUserLink))
    assert link.fhir_user == "Practitioner/prac-1" and link.user_id == setup["physician_user"]["user_id"]
    [entry] = await audit_rows(db, "SMART_LAUNCH")
    assert entry.resource_id == setup["patient"]["patient_id"] and "MRN" not in entry.details

    # The session works like any other.
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["role"] == "physician"


async def test_launch_state_is_single_use_and_pkce_is_checked(client, setup, ehr, db):
    bad, _ = await do_launch(client, ehr, verifier="w" * 50)
    assert bad.status_code == 400 and "PKCE" in bad.json()["detail"]
    [failed] = await audit_rows(db, "SMART_LAUNCH_FAILED")
    assert "PKCE" in failed.details
    launch = await db.scalar(select(SmartLaunch))
    await db.refresh(launch)
    assert launch.status == "failed" and launch.access_token is None

    ok, query = await do_launch(client, ehr)
    assert ok.status_code == 200
    again = await client.post("/api/smart/token", json={"state": query["state"], "code": "code-x",
                                                       "code_verifier": VERIFIER})
    assert again.status_code == 400


@pytest.mark.parametrize("claims, reason", [
    ({"aud": "someone-else"}, "InvalidAudienceError"),
    ({"iss": "https://other.example"}, "InvalidIssuerError"),
    ({"nonce": "not-the-nonce"}, "nonce mismatch"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"fhirUser": None}, "fhirUser"),
])
async def test_id_token_is_verified(client, setup, ehr, claims, reason):
    ehr.id_claims = claims
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 401 and reason in resp.json()["detail"]
    assert "df_refresh=" not in resp.headers.get("set-cookie", "").replace("df_refresh=;", "")


async def test_id_token_signed_by_another_key_is_refused(client, setup, ehr):
    ehr.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 401


async def test_unknown_ehr_user_needs_email_or_admin_link(client, setup, ehr, db):
    ehr.practitioner_email = None
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 403 and "link" in resp.json()["detail"]

    linked = await client.post("/api/smart/links", headers=setup["admin"], json={
        "issuer_id": setup["issuer"]["issuer_id"], "fhir_user": "Practitioner/prac-1",
        "user_id": setup["physician_user"]["user_id"]})
    assert linked.status_code == 200, linked.text
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 200 and resp.json()["user"]["user_id"] == setup["physician_user"]["user_id"]


async def test_unknown_email_becomes_pending_without_patient_context(client, setup, ehr, db):
    ehr.practitioner_email = "new.doctor@hospital.example"
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["role"] == "pending" and body["launch"]["matched"] is False
    launch = await db.scalar(select(SmartLaunch))
    assert launch.access_token is None and launch.patient_id is None
    assert len(await audit_rows(db, "REQUEST_ACCESS")) == 1
    assert not any(r.url.path.startswith("/fhir/Patient") for r in ehr.requests)


async def test_unmatched_mrn_still_signs_in(client, setup, ehr):
    ehr.patient_identifiers = [{"system": "urn:oid:1.2.3.mrn", "value": "MRN-UNKNOWN"}]
    resp, _ = await do_launch(client, ehr)
    assert resp.status_code == 200
    assert resp.json()["launch"]["matched"] is False and "No DischargeFlow patient" in resp.json()["launch"]["note"]


async def test_issuer_admin_requires_user_manage(client, make_user):
    _, nurse = await make_user("nurse")
    assert (await client.get("/api/smart/issuers", headers=nurse)).status_code == 403
    _, admin = await make_user("admin")
    bad = await client.post("/api/smart/issuers", headers=admin, json={
        "iss": "http://ehr.example/fhir", "name": "x", "client_id": "c", "redirect_uri": REDIRECT})
    assert bad.status_code == 400


# ---------- item 15: clinical context ----------

async def test_clinical_context_is_read_live_audited_and_session_bound(client, setup, ehr, db, make_user):
    resp, _ = await do_launch(client, ehr)
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    pid = setup["patient"]["patient_id"]
    reads_before = len(await audit_rows(db, "PHI_READ"))

    ctx = (await client.get(f"/api/smart/clinical-context/{pid}", headers=headers)).json()
    assert ctx["available"] is True
    assert [m["name"] for m in ctx["medications"]] == ["Lisinopril 10 MG Oral Tablet", "Metformin 500 MG Oral Tablet"]
    assert ctx["medications"][0]["dosage"] == "10 mg by mouth once daily"
    assert [p["name"] for p in ctx["problems"]] == ["Type 2 diabetes mellitus", "Chronic systolic heart failure",
                                                    "Hypertension"]  # refuted one left out
    # Charlson flags proposed from the codes, each with the condition it came from (refuted dementia is not).
    assert ctx["charlson_proposals"] == [
        {"key": "diabetes_uncomplicated", "label": "Diabetes without complications", "weight": 1, "sources": [
            {"condition": "Type 2 diabetes mellitus", "system": "ICD-10-CM", "code": "E11.9"},
            {"condition": "Type 2 diabetes mellitus", "system": "SNOMED CT", "code": "44054006"}]},
        {"key": "congestive_heart_failure", "label": "Congestive heart failure", "weight": 2, "sources": [
            {"condition": "Chronic systolic heart failure", "system": "ICD-10-CM", "code": "I50.22"}]}]
    assert "Quan" in ctx["charlson_citation"]
    assert all("codings" not in p for p in ctx["problems"])
    assert ctx["allergies"] == [{"substance": "Penicillin", "criticality": "high", "reactions": ["Hives"]}]
    assert EHR_TOKEN not in json.dumps(ctx)

    calls = ehr.fhir_calls()
    again = (await client.get(f"/api/smart/clinical-context/{pid}", headers=headers)).json()
    assert again["available"] is True and ehr.fhir_calls() == calls  # served from the short in-process cache
    reads = (await audit_rows(db, "PHI_READ"))[reads_before:]
    assert len(reads) == 2 and all(r.resource_id == pid and "2 medications" in r.details
                                   and "2 Charlson proposals" in r.details for r in reads)

    # Another session (the same physician signed in elsewhere, or someone else) can't use this launch's token.
    _, other = await make_user("physician")
    assert (await client.get(f"/api/smart/clinical-context/{pid}", headers=other)).json()["reason"] == "no_launch"
    assert (await client.get(f"/api/smart/clinical-context/{pid}", headers=setup["physician"])).json()[
        "available"] is False
    assert (await client.get("/api/smart/clinical-context/pat_missing", headers=headers)).status_code == 404


async def test_ehr_refusing_the_token_ends_the_launch(client, setup, ehr, db):
    resp, _ = await do_launch(client, ehr)
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    ehr.revoked = True
    ctx = (await client.get(f"/api/smart/clinical-context/{setup['patient']['patient_id']}", headers=headers)).json()
    assert ctx["available"] is False and ctx["reason"] == "expired"
    launch = await db.scalar(select(SmartLaunch))
    await db.refresh(launch)
    assert launch.status == "ended" and launch.access_token is None and launch.dek is None


async def test_disabling_the_issuer_ends_its_launches(client, setup, ehr, db):
    resp, _ = await do_launch(client, ehr)
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    issuer = setup["issuer"]
    upd = await client.put(f"/api/smart/issuers/{issuer['issuer_id']}", headers=setup["admin"], json={
        **{k: issuer[k] for k in ("iss", "name", "client_id", "redirect_uri", "mrn_system", "mrn_authority",
                                  "visit_system")}, "enabled": False})
    assert upd.status_code == 200 and upd.json()["enabled"] is False
    ctx = (await client.get(f"/api/smart/clinical-context/{setup['patient']['patient_id']}", headers=headers)).json()
    assert ctx["available"] is False
    refused = await client.post("/api/smart/launch", json={"iss": ISS, "launch": "x",
                                                          "code_challenge": smart.s256(VERIFIER)})
    assert refused.status_code == 403


async def test_summary_is_grounded_in_the_ehr_lists(client, setup, ehr, db, monkeypatch):
    seen: list[dict] = []

    async def spy(db, user, patient):
        ctx = await smart.summary_context(db, user, patient)
        seen.append(ctx)
        return ctx

    monkeypatch.setattr(ai_service, "_clinical_context_source", spy)
    resp, _ = await do_launch(client, ehr)
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    pid = setup["patient"]["patient_id"]
    wf = await client.post(f"/api/workflows/initiate/{pid}", headers=headers)
    assert wf.status_code == 200, wf.text
    gen = await client.post("/api/ai/generate-summary", headers=headers, json={
        "patient_id": pid, "workflow_id": wf.json()["workflow"]["workflow_id"],
        "clinical_notes": "Medications: penicillin 500 mg four times daily"})
    assert gen.status_code == 200, gen.text
    content = gen.json()["content"].lower()
    assert "lisinopril" in content and "metformin" in content and "diabetes" in content and "penicillin" in content
    # The allergy check flags a prescribed medication named like a recorded allergy (lexically: it would not know
    # that amoxicillin is a penicillin, so the test uses the allergen's own name).
    assert [w["type"] for w in gen.json()["generation"]["grounding_warnings"]] == ["allergy"]
    reads = await audit_rows(db, "PHI_READ")
    assert any("EHR clinical context (summary)" in r.details for r in reads)
    # Risk proposals never reach the model's input.
    assert seen and seen[0]["problems"] and not any("charlson" in k for k in seen[0])


# ---------- units ----------

def test_pkce_challenge_matches_the_browser():
    # frontend/src/components/smart.test.jsx expects the same value from crypto.subtle.
    assert smart.s256("dBjftJeZ4CVP-mJ92K9mHBaB7qYcRlG3xh1lX8qhQyUe8EOs") == "rPQ5JFnsLOVWO0OE9P42S4wLQjxTT5ZYOYigrxZk4io"


def test_grounding_flags_a_medication_matching_an_allergy():
    doc = DischargeSummaryDoc.model_validate({
        "hospital_course": "x", "discharge_diagnoses": ["Cellulitis"], "procedures": [], "activity": "", "diet": "",
        "follow_up": [], "warning_signs": [],
        "medications": [{"name": "Penicillin VK", "dose": "", "frequency": "", "instructions": ""}]})
    source = "penicillin vk cellulitis"
    assert check_grounding(doc, source) == []
    assert check_grounding(doc, source, ["Penicillin (Hives)"])[0]["type"] == "allergy"


async def test_fhir_client_does_not_follow_links_to_other_hosts():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [],
                                         "link": [{"relation": "next", "url": "https://evil.example/steal"}]})

    original = fhir_client.http_client
    fhir_client.http_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await fhir_client.FhirClient(ISS, "t").search("Condition", {"patient": "p"}) == []
        with pytest.raises(fhir_client.FhirError):
            await fhir_client.FhirClient(ISS, "t").read("https://evil.example/fhir/Patient/1")
    finally:
        fhir_client.http_client = original
    assert len(seen) == 1 and seen[0].startswith(ISS)
