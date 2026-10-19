"""SMART on FHIR EHR launch (ADR-002 item 14) and read-only clinical context from the EHR (item 15).

Launch (SMART App Launch 2.x, EHR launch, public client with PKCE):

1. The EHR opens `/launch?iss=...&launch=...`. The browser makes a PKCE verifier, keeps it in sessionStorage and
   posts `iss`, `launch` and the S256 challenge to `POST /api/smart/launch`.
2. `start_launch` refuses an `iss` that is not on the tenant's allow-list (`smart_issuers`), discovers the endpoints
   from `{iss}/.well-known/smart-configuration`, stores a pending launch (hash of a fresh `state`, the challenge, a
   nonce) and returns the authorization URL with scopes `launch openid fhirUser patient/*.read`.
3. The authorization server redirects back to `/launch?code=...&state=...`. The browser checks `state` against the
   one it kept and posts code, state and verifier to `POST /api/smart/token`.
4. `exchange` finds the pending launch by the state's hash (single use, 10 minutes), checks the verifier against the
   stored challenge, exchanges the code at the token endpoint, verifies the id_token (signature by a key from the
   server's JWKS, `iss`, `aud` = client id, expiry, nonce), maps `fhirUser` to a DischargeFlow user, maps the EHR's
   patient and encounter to ours through the MRN blind index and the visit number, and starts our normal session.

The EHR access token never reaches the browser: it is stored encrypted under the launch's own data key, used only
by the session that made the launch, and dropped when it expires or the EHR refuses it.
"""
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit_phi_read, write_audit
from app.core import crypto
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.core.tenancy import use_tenant
from app.discharge.service import active_workflow_for
from app.identity import sessions
from app.identity import service as identity_service
from app.identity.models import User
from app.identity.permissions import PENDING_ROLE
from app.interop import fhir_client
from app.interop.adt import find_patient
from app.interop.fhir_client import ClinicalContext, FhirClient, FhirError
from app.interop.models import EhrUserLink, SmartIssuer, SmartLaunch
from app.patients.models import Encounter, Patient
from app.patients.service import current_encounter

SCOPES = "launch openid fhirUser patient/*.read"
LAUNCH_TTL = timedelta(minutes=10)
EHR_TOKEN_DEFAULT_SECONDS = 300
EHR_TOKEN_MAX_SECONDS = 3600
ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "ES256", "ES384"]
LEEWAY_SECONDS = 60
JWKS_CACHE_SECONDS = 3600
SYSTEM_ROLE = "system"
CONTEXT_SOURCE = "EHR (SMART on FHIR)"


class SmartError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def normalize_iss(iss: str) -> str:
    return iss.strip().rstrip("/")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def acceptable_url(url: str) -> bool:
    """https, or http to localhost outside production (local EHR sandboxes and the tests' fake server)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc:
        return True
    return (parts.scheme == "http" and not settings.is_production
            and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1"))


def _actor(issuer: SmartIssuer) -> CurrentUser:
    """Who audit entries name before a person is known (the launch itself, by issuer)."""
    return CurrentUser(user_id=issuer.issuer_id, email="", name=f"SMART launch ({issuer.name})"[:200],
                       role=SYSTEM_ROLE, tenant_id=issuer.tenant_id, session_id="", created_at=issuer.created_at)


# ---------- allow-list ----------

async def issuer_for(db: AsyncSession, iss: str) -> Optional[SmartIssuer]:
    return await db.scalar(select(SmartIssuer).where(SmartIssuer.iss == normalize_iss(iss)))


# ---------- discovery ----------

async def discover(iss: str) -> dict:
    """The server's SMART configuration, with the fields the launch needs checked."""
    try:
        async with fhir_client.http_client() as http:
            resp = await http.get(f"{iss}/.well-known/smart-configuration", headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise SmartError(502, f"The EHR's SMART configuration could not be read ({type(e).__name__})")
    if resp.status_code != 200:
        raise SmartError(502, f"The EHR's SMART configuration could not be read (HTTP {resp.status_code})")
    try:
        config = resp.json()
    except ValueError:
        config = None
    if not isinstance(config, dict):
        raise SmartError(502, "The EHR's SMART configuration is not valid JSON")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(config.get(key), str) or not acceptable_url(config[key]):
            raise SmartError(502, f"The EHR's SMART configuration has no usable {key}")
    methods = config.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise SmartError(502, "The EHR does not support PKCE with S256")
    jwks_uri = config.get("jwks_uri")
    if jwks_uri is not None and (not isinstance(jwks_uri, str) or not acceptable_url(jwks_uri)):
        raise SmartError(502, "The EHR's SMART configuration has an unusable jwks_uri")
    return config


# ---------- 1-2: start ----------

@dataclass
class Started:
    authorize_url: str
    state: str
    launch_id: str


async def start_launch(db: AsyncSession, iss: str, launch: str, code_challenge: str) -> Started:
    await use_tenant(db, sessions.default_tenant())
    issuer = await issuer_for(db, iss)
    if issuer is None or not issuer.enabled:
        # Nothing is fetched from an unknown server.
        raise SmartError(403, "This EHR is not allowed to launch DischargeFlow. Ask an administrator to register it.")
    config = await discover(issuer.iss)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    row = SmartLaunch(issuer_id=issuer.issuer_id, state_hash=_sha256(state), code_challenge=code_challenge,
                      nonce=nonce, authorize_endpoint=config["authorization_endpoint"],
                      token_endpoint=config["token_endpoint"], jwks_uri=config.get("jwks_uri"),
                      oidc_issuer=normalize_iss(config["issuer"]) if isinstance(config.get("issuer"), str) else None,
                      expires_at=utcnow() + LAUNCH_TTL)
    db.add(row)
    await db.flush()
    query = urlencode({
        "response_type": "code", "client_id": issuer.client_id, "redirect_uri": issuer.redirect_uri,
        "launch": launch, "scope": SCOPES, "state": state, "aud": issuer.iss, "nonce": nonce,
        "code_challenge": code_challenge, "code_challenge_method": "S256",
    })
    sep = "&" if "?" in row.authorize_endpoint else "?"
    return Started(authorize_url=f"{row.authorize_endpoint}{sep}{query}", state=state, launch_id=row.launch_id)


# ---------- 4: token exchange ----------

_jwks_cache: dict[str, tuple[float, dict]] = {}


async def _jwks(uri: str, refresh: bool = False) -> dict:
    hit = _jwks_cache.get(uri)
    if hit and hit[0] > time.monotonic() and not refresh:
        return hit[1]
    try:
        async with fhir_client.http_client() as http:
            resp = await http.get(uri, headers={"Accept": "application/json"})
        keys = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        keys = None
    if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
        raise SmartError(401, "EHR sign-in refused: its signing keys could not be read")
    _jwks_cache[uri] = (time.monotonic() + JWKS_CACHE_SECONDS, keys)
    return keys


async def _signing_key(uri: str, kid: Optional[str]) -> jwt.PyJWK:
    for refresh in (False, True):  # an unknown kid (key rotation) refetches once
        try:
            keyset = jwt.PyJWKSet.from_dict(await _jwks(uri, refresh))
        except jwt.PyJWKSetError:
            continue
        for key in keyset.keys:
            if kid is None or key.key_id == kid:
                return key
    raise SmartError(401, "EHR sign-in refused: signing key not found")


async def verify_id_token(token: str, launch: SmartLaunch, issuer: SmartIssuer) -> dict:
    if not launch.jwks_uri:
        raise SmartError(401, "EHR sign-in refused: the EHR publishes no jwks_uri to verify its id_token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise SmartError(401, "EHR sign-in refused: id_token is not a JWT")
    key = await _signing_key(launch.jwks_uri, header.get("kid"))
    try:
        claims = jwt.decode(token, key.key, algorithms=ID_TOKEN_ALGORITHMS, audience=issuer.client_id,
                            issuer=launch.oidc_issuer or issuer.iss, leeway=LEEWAY_SECONDS,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.ExpiredSignatureError:
        raise SmartError(401, "EHR sign-in refused: id_token expired")
    except jwt.InvalidTokenError as e:
        raise SmartError(401, f"EHR sign-in refused: {type(e).__name__}")
    # SMART servers need not echo a nonce; one that does must echo ours.
    if "nonce" in claims and not hmac.compare_digest(str(claims["nonce"]), launch.nonce):
        raise SmartError(401, "EHR sign-in refused: nonce mismatch")
    fhir_user = claims.get("fhirUser") or claims.get("profile")
    if not isinstance(fhir_user, str) or not fhir_user.strip():
        raise SmartError(401, "EHR sign-in refused: no fhirUser claim (the fhirUser scope was not granted)")
    return claims


async def _token_request(launch: SmartLaunch, issuer: SmartIssuer, code: str, verifier: str) -> dict:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": issuer.redirect_uri,
            "client_id": issuer.client_id, "code_verifier": verifier}
    try:
        async with fhir_client.http_client() as http:
            resp = await http.post(launch.token_endpoint, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise SmartError(502, f"The EHR's token endpoint could not be reached ({type(e).__name__})")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code != 200 or not isinstance(body, dict):
        error = body.get("error") if isinstance(body, dict) else None
        raise SmartError(401, f"The EHR refused the authorization code ({error or f'HTTP {resp.status_code}'})")
    if not body.get("access_token") or str(body.get("token_type", "")).lower() != "bearer":
        raise SmartError(502, "The EHR's token response has no bearer access token")
    if not body.get("id_token"):
        raise SmartError(401, "EHR sign-in refused: no id_token (the openid scope was not granted)")
    return body


def _email_from(resource: dict) -> Optional[str]:
    for telecom in resource.get("telecom") or []:
        if isinstance(telecom, dict) and telecom.get("system") == "email" and "@" in str(telecom.get("value", "")):
            return str(telecom["value"]).strip()
    return None


def _name_from(resource: dict) -> Optional[str]:
    for name in resource.get("name") or []:
        if not isinstance(name, dict):
            continue
        if name.get("text"):
            return str(name["text"]).strip()
        parts = [*(name.get("given") or []), name.get("family") or ""]
        joined = " ".join(str(p) for p in parts if p).strip()
        if joined:
            return joined
    return None


async def _user_for(db: AsyncSession, issuer: SmartIssuer, claims: dict,
                    fhir: FhirClient) -> tuple[User, bool]:
    """The DischargeFlow user for the id_token's fhirUser: a link first, else a match by email (the Google login's
    rules: unknown → `pending` if registration allows it), taking the email from the id_token or, failing that, from
    the fhirUser resource (Practitioner.telecom). Returns (user, newly registered)."""
    fhir_user = str(claims.get("fhirUser") or claims.get("profile")).strip()
    link = await db.scalar(select(EhrUserLink).where(EhrUserLink.issuer_id == issuer.issuer_id,
                                                     EhrUserLink.fhir_user == fhir_user))
    if link is not None:
        user = await db.get(User, link.user_id)
        if user is not None:
            return user, False

    email = claims.get("email") if claims.get("email_verified") not in (False, "false") else None
    name = claims.get("name")
    if not email or not name:
        try:
            resource = await fhir.read(fhir_user)
        except FhirError:
            resource = {}
        email = email or _email_from(resource)
        name = name or _name_from(resource)
    if not email:
        raise SmartError(403, "Your EHR account is not linked to a DischargeFlow user and has no email to match. "
                              "Ask an administrator to link it.")
    user, registered = await identity_service.match_or_register(db, email, name or email.split("@")[0], None)
    db.add(EhrUserLink(issuer_id=issuer.issuer_id, fhir_user=fhir_user, user_id=user.user_id))
    await db.flush()
    return user, registered


def _identifier(resource: dict, system: Optional[str], type_code: Optional[str] = None) -> Optional[str]:
    for ident in resource.get("identifier") or []:
        if not isinstance(ident, dict) or not ident.get("value"):
            continue
        if system and ident.get("system") == system:
            return str(ident["value"]).strip()
        if not system and type_code and type_code in {c.get("code") for c in (ident.get("type") or {}).get("coding")
                                                      or [] if isinstance(c, dict)}:
            return str(ident["value"]).strip()
    return None


@dataclass
class MappedContext:
    patient: Optional[Patient] = None
    encounter: Optional[Encounter] = None
    note: Optional[str] = None


async def map_context(db: AsyncSession, issuer: SmartIssuer, fhir: FhirClient, ehr_patient: Optional[str],
                      ehr_encounter: Optional[str]) -> MappedContext:
    """Our patient and encounter for the EHR's launch context, or a note saying why there is none."""
    if not ehr_patient:
        return MappedContext(note="The EHR sent no patient context")
    try:
        resource = await fhir.read(f"Patient/{ehr_patient}")
    except FhirError as e:
        return MappedContext(note=f"The EHR's Patient could not be read ({e})")
    # The MRN: the configured identifier system, else an identifier typed MR (HL7 v2 table 0203).
    mrn = _identifier(resource, issuer.mrn_system, None if issuer.mrn_system else "MR")
    if not mrn:
        return MappedContext(note="The EHR's Patient has no MRN identifier")
    patient = await find_patient(db, issuer.tenant_id, issuer.mrn_authority, mrn)
    if patient is None:
        return MappedContext(note="No DischargeFlow patient has this MRN (not admitted through the ADT feed yet)")

    encounter = None
    if ehr_encounter and issuer.visit_system:
        try:
            visit = _identifier(await fhir.read(f"Encounter/{ehr_encounter}"), issuer.visit_system)
        except FhirError:
            visit = None
        if visit:
            stays = await db.scalars(select(Encounter).where(Encounter.patient_id == patient.patient_id,
                                                             Encounter.status != "cancelled"))
            # Visit numbers are encrypted with the patient's key: compared after decrypting (few rows).
            encounter = next((e for e in stays if e.visit_number == visit), None)
    encounter = encounter or await current_encounter(db, patient.patient_id)
    return MappedContext(patient=patient, encounter=encounter)


def _aad(launch: SmartLaunch, field: str) -> str:
    return f"{launch.launch_id}:{field}"


def end_launch(launch: SmartLaunch, status: str, failure: Optional[str] = None) -> None:
    launch.status = status
    launch.dek = None
    launch.access_token = None
    launch.ehr_patient = None
    launch.ehr_encounter = None
    launch.ended_at = utcnow()
    if failure:
        launch.failure = failure[:200]
    fhir_client.forget(launch.launch_id)


@dataclass
class Exchanged:
    body: dict  # our normal token response, plus `launch`


async def pending_launch(db: AsyncSession, state: str) -> tuple[SmartLaunch, SmartIssuer]:
    await use_tenant(db, sessions.default_tenant())
    launch = await db.scalar(select(SmartLaunch).where(SmartLaunch.state_hash == _sha256(state)))
    if launch is None:
        raise SmartError(400, "Unknown or already used launch. Launch DischargeFlow from the EHR again.")
    issuer = await db.get(SmartIssuer, launch.issuer_id)
    if issuer is None or not issuer.enabled:
        raise SmartError(403, "This EHR is no longer allowed to launch DischargeFlow")
    return launch, issuer


async def exchange(db: AsyncSession, request: Request, response, launch: SmartLaunch, issuer: SmartIssuer,
                   code: str, verifier: str) -> dict:
    if launch.status != "pending":
        raise SmartError(400, "This launch was already used. Launch DischargeFlow from the EHR again.")
    if launch.expires_at <= utcnow():
        raise SmartError(400, "This launch expired. Launch DischargeFlow from the EHR again.")
    # Single use from here on, whatever happens next.
    launch.status = "failed"
    if not hmac.compare_digest(s256(verifier), launch.code_challenge):
        raise SmartError(400, "PKCE verification failed")

    token = await _token_request(launch, issuer, code, verifier)
    claims = await verify_id_token(token["id_token"], launch, issuer)
    fhir = FhirClient(issuer.iss, str(token["access_token"]))
    user, registered = await _user_for(db, issuer, claims, fhir)
    session, refresh_token = await identity_service.start_user_session(db, request, user, registered)
    actor = identity_service.identity_of(user, session)

    launch.status = "active"
    launch.completed_at = utcnow()
    launch.session_id, launch.user_id = session.session_id, user.user_id
    launch.fhir_user = str(claims.get("fhirUser") or claims.get("profile"))[:500]
    launch.scope = str(token.get("scope") or "")[:1000]
    ehr_patient, ehr_encounter = token.get("patient"), token.get("encounter")
    context: dict = {"launch_id": launch.launch_id, "patient_id": None, "encounter_id": None, "workflow_id": None,
                     "matched": False, "note": None}
    if user.role == PENDING_ROLE:
        # No access to patient data yet: nothing is looked up and the EHR token is not kept.
        end_launch(launch, "ended", "user pending approval")
        context["note"] = "Your account is waiting for approval"
    else:
        mapped = await map_context(db, issuer, fhir, ehr_patient, ehr_encounter)
        try:
            expires_in = int(token.get("expires_in") or EHR_TOKEN_DEFAULT_SECONDS)
        except (TypeError, ValueError):
            expires_in = EHR_TOKEN_DEFAULT_SECONDS
        launch.expires_at = utcnow() + timedelta(seconds=max(30, min(expires_in, EHR_TOKEN_MAX_SECONDS)))
        launch.dek = crypto.new_wrapped_dek()
        launch.access_token = crypto.encrypt_field(launch.dek, str(token["access_token"]), _aad(launch, "token"))
        if ehr_patient:
            launch.ehr_patient = crypto.encrypt_field(launch.dek, str(ehr_patient), _aad(launch, "patient"))
        if ehr_encounter:
            launch.ehr_encounter = crypto.encrypt_field(launch.dek, str(ehr_encounter), _aad(launch, "encounter"))
        launch.context_note = mapped.note
        context["note"] = mapped.note
        if mapped.patient is not None:
            launch.patient_id = mapped.patient.patient_id
            launch.encounter_id = mapped.encounter.encounter_id if mapped.encounter else None
            workflow = await active_workflow_for(db, mapped.patient.patient_id)
            context.update(patient_id=mapped.patient.patient_id, encounter_id=launch.encounter_id,
                           workflow_id=workflow.workflow_id if workflow else None, matched=True)
    await write_audit(db, actor, "SMART_LAUNCH", "patient" if launch.patient_id else "smart_launch",
                      launch.patient_id or launch.launch_id,
                      f"launch {launch.launch_id} issuer {issuer.issuer_id} "
                      f"{'patient matched' if launch.patient_id else 'no patient match'}", request)
    body = await identity_service.token_response(db, response, user, session, refresh_token)
    return {**body, "launch": context}


async def record_failure(db: AsyncSession, launch_id: str, issuer_id: str, detail: str,
                         request: Request) -> None:
    """Called by the router after a failed exchange, once the failed transaction has rolled back: the launch can't
    be tried again, and the failure is audited (the reason only)."""
    await use_tenant(db, sessions.default_tenant())
    issuer = await db.get(SmartIssuer, issuer_id)
    row = await db.get(SmartLaunch, launch_id)
    if row is not None and row.status in ("pending", "failed"):
        end_launch(row, "failed", detail)
    if issuer is not None:
        await write_audit(db, _actor(issuer), "SMART_LAUNCH_FAILED", "smart_launch", launch_id, detail[:300],
                          request)


# ---------- item 15: clinical context ----------

async def active_launch(db: AsyncSession, user: CurrentUser, patient_id: str) -> Optional[SmartLaunch]:
    """The live launch this session made for this patient, if any. A launch from another session (another person
    at the workstation after the idle lock, say) is never used."""
    rows = list(await db.scalars(select(SmartLaunch).where(
        SmartLaunch.session_id == user.session_id, SmartLaunch.patient_id == patient_id,
        SmartLaunch.status == "active").order_by(SmartLaunch.completed_at.desc())))
    live = None
    for row in rows:
        if row.expires_at <= utcnow() or row.dek is None or row.access_token is None:
            end_launch(row, "ended", "EHR token expired")
        elif live is None:
            live = row
    return live


async def _fetch(db: AsyncSession, launch: SmartLaunch) -> Optional[ClinicalContext]:
    hit = fhir_client.cached(launch.launch_id)
    if hit is not None:
        return hit
    issuer = await db.get(SmartIssuer, launch.issuer_id)
    if issuer is None or not issuer.enabled or launch.ehr_patient is None:
        return None
    assert launch.dek is not None and launch.access_token is not None
    client = FhirClient(issuer.iss, crypto.decrypt_field(launch.dek, launch.access_token, _aad(launch, "token")))
    ctx = await fhir_client.fetch_clinical_context(
        client, crypto.decrypt_field(launch.dek, launch.ehr_patient, _aad(launch, "patient")))
    if ctx.unauthorized:
        end_launch(launch, "ended", "EHR refused the token")
        return None
    fhir_client.remember(launch.launch_id, ctx)
    return ctx


async def clinical_context(db: AsyncSession, user: CurrentUser, patient: Patient,
                           request: Optional[Request] = None, purpose: str = "view") -> dict:
    """What the screens show. Audited as a PHI read whenever EHR data is returned (cache hits too)."""
    launch = await active_launch(db, user, patient.patient_id)
    if launch is None:
        return {"available": False, "reason": "no_launch",
                "detail": "Open this patient from the EHR to see its medication, problem and allergy lists."}
    ctx = await _fetch(db, launch)
    if ctx is None:
        return {"available": False, "reason": "expired",
                "detail": "The EHR session ended. Launch DischargeFlow from the EHR again."}
    if len(ctx.errors) == len(fhir_client.SEARCHES):
        return {"available": False, "reason": "unavailable", "detail": "The EHR did not answer.",
                "errors": ctx.errors}
    await audit_phi_read(db, user, patient.patient_id, f"EHR clinical context ({purpose}): {ctx.counts()}", request)
    return {"available": True, "source": CONTEXT_SOURCE, "launch_id": launch.launch_id,
            "fetched_at": datetime.fromtimestamp(ctx.fetched_at, tz=timezone.utc).isoformat(),
            **ctx.as_dict()}


async def summary_context(db: AsyncSession, user: CurrentUser, patient: Patient) -> Optional[dict]:
    """The clinical context for a discharge summary (registered with ai.service by app.main), or None."""
    view = await clinical_context(db, user, patient, purpose="summary")
    return view if view.get("available") else None


# ---------- admin ----------

def issuer_view(issuer: SmartIssuer) -> dict:
    return issuer.to_dict()


async def ensure_unique_iss(db: AsyncSession, iss: str, except_id: Optional[str] = None) -> None:
    existing = await issuer_for(db, iss)
    if existing is not None and existing.issuer_id != except_id:
        raise SmartError(409, "This FHIR server is already registered")


async def audit_admin(db: AsyncSession, user: CurrentUser, action: str, resource_id: str, details: str,
                      request: Optional[Request]) -> None:
    await write_audit(db, user, action, "smart_issuer", resource_id, details[:500], request)

