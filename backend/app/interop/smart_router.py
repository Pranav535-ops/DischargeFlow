"""SMART on FHIR EHR launch and EHR clinical context (ADR-002 items 14 and 15). See interop/smart.py."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.db import DB
from app.core.security import AuthUser, CurrentUser, requires
from app.identity.models import User
from app.interop import smart
from app.interop.models import EhrUserLink, SmartIssuer, SmartLaunch
from app.patients.models import Patient
from app.patients.service import restricted_response

router = APIRouter(prefix="/smart", tags=["SMART on FHIR"])

B64URL = r"^[A-Za-z0-9_-]+$"


class LaunchStart(BaseModel):
    iss: str = Field(min_length=8, max_length=500)
    launch: str = Field(min_length=1, max_length=2000)
    # S256 of a 43-128 character verifier: 43 base64url characters.
    code_challenge: str = Field(min_length=43, max_length=128, pattern=B64URL)


class TokenExchange(BaseModel):
    state: str = Field(min_length=16, max_length=200)
    code: str = Field(min_length=1, max_length=4000)
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")


class IssuerIn(BaseModel):
    iss: str = Field(min_length=8, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    redirect_uri: str = Field(min_length=8, max_length=500)
    mrn_system: Optional[str] = Field(default=None, max_length=500)
    mrn_authority: Optional[str] = Field(default=None, max_length=60)
    visit_system: Optional[str] = Field(default=None, max_length=500)
    enabled: bool = True


class LinkIn(BaseModel):
    issuer_id: str = Field(min_length=1, max_length=40)
    fhir_user: str = Field(min_length=3, max_length=500)
    user_id: str = Field(min_length=1, max_length=40)


def _refused(e: smart.SmartError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail})


@router.post("/launch")
async def start_launch(body: LaunchStart, db: DB):
    """Step 1 of an EHR launch (public: no one is signed in yet). Refuses an `iss` not on the allow-list."""
    try:
        started = await smart.start_launch(db, body.iss, body.launch, body.code_challenge)
    except smart.SmartError as e:
        return _refused(e)
    return {"authorize_url": started.authorize_url, "state": started.state}


@router.post("/token")
async def exchange_token(body: TokenExchange, request: Request, response: Response, db: DB):
    """Step 2: exchange the authorization code; returns our normal session (access token + df_refresh cookie) and
    the mapped patient context. The EHR's tokens stay on the server."""
    try:
        launch, issuer = await smart.pending_launch(db, body.state)
    except smart.SmartError as e:
        return _refused(e)
    launch_id, issuer_id = launch.launch_id, issuer.issuer_id
    try:
        return await smart.exchange(db, request, response, launch, issuer, body.code, body.code_verifier)
    except (smart.SmartError, HTTPException) as e:
        error = e if isinstance(e, smart.SmartError) else smart.SmartError(e.status_code, str(e.detail))
    # Nothing of the failed attempt is kept; the launch is marked used and the failure audited.
    await db.rollback()
    await smart.record_failure(db, launch_id, issuer_id, error.detail, request)
    return _refused(error)


@router.get("/clinical-context/{patient_id}")
async def get_clinical_context(patient_id: str, request: Request, db: DB, user: AuthUser):
    """Active medications, problems and allergies from the EHR for the patient this session launched with
    (`available: false` otherwise). Read live from the EHR (cached briefly in memory, never stored); a PHI read."""
    patient = await db.get(Patient, patient_id)
    if patient is None:
        return await restricted_response(db, user, patient_id) or JSONResponse(
            status_code=404, content={"detail": "Patient not found"})
    return await smart.clinical_context(db, user, patient, request)


# ---------- admin: the allow-list and user links ----------

def _check_urls(body: IssuerIn) -> None:
    for field in ("iss", "redirect_uri"):
        if not smart.acceptable_url(getattr(body, field).strip()):
            raise HTTPException(status_code=400, detail=f"{field} must be an https URL")


@router.get("/issuers")
async def list_issuers(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    return [smart.issuer_view(i) for i in await db.scalars(select(SmartIssuer).order_by(SmartIssuer.created_at))]


@router.post("/issuers")
async def create_issuer(body: IssuerIn, request: Request, db: DB,
                        user: CurrentUser = Depends(requires("user:manage"))):
    _check_urls(body)
    iss = smart.normalize_iss(body.iss)
    try:
        await smart.ensure_unique_iss(db, iss)
    except smart.SmartError as e:
        return _refused(e)
    issuer = SmartIssuer(**{**body.model_dump(), "iss": iss, "redirect_uri": body.redirect_uri.strip()},
                         created_by=user.user_id)
    db.add(issuer)
    await db.flush()
    await smart.audit_admin(db, user, "CREATE_SMART_ISSUER", issuer.issuer_id, f"{issuer.name}: {iss}", request)
    return smart.issuer_view(issuer)


@router.put("/issuers/{issuer_id}")
async def update_issuer(issuer_id: str, body: IssuerIn, request: Request, db: DB,
                        user: CurrentUser = Depends(requires("user:manage"))):
    issuer = await db.get(SmartIssuer, issuer_id)
    if issuer is None:
        raise HTTPException(status_code=404, detail="Issuer not found")
    _check_urls(body)
    iss = smart.normalize_iss(body.iss)
    try:
        await smart.ensure_unique_iss(db, iss, issuer_id)
    except smart.SmartError as e:
        return _refused(e)
    changed = sorted(k for k, v in {**body.model_dump(), "iss": iss}.items() if getattr(issuer, k) != v)
    for key, value in {**body.model_dump(), "iss": iss, "redirect_uri": body.redirect_uri.strip()}.items():
        setattr(issuer, key, value)
    if not issuer.enabled:
        # Disabling stops the EHR tokens already issued through it from being used.
        for launch in await db.scalars(select(SmartLaunch).where(SmartLaunch.issuer_id == issuer_id,
                                                                 SmartLaunch.status.in_(("pending", "active")))):
            smart.end_launch(launch, "ended", "issuer disabled")
    await smart.audit_admin(db, user, "UPDATE_SMART_ISSUER", issuer_id, f"changed: {', '.join(changed) or 'none'}",
                            request)
    return smart.issuer_view(issuer)


@router.get("/links")
async def list_links(db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    return [link.to_dict() for link in await db.scalars(select(EhrUserLink).order_by(EhrUserLink.created_at))]


@router.post("/links")
async def create_link(body: LinkIn, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    """Link an EHR user (fhirUser, e.g. Practitioner/123) to a DischargeFlow user, for EHR accounts with no email."""
    if await db.get(SmartIssuer, body.issuer_id) is None:
        raise HTTPException(status_code=400, detail="Unknown issuer")
    if await db.get(User, body.user_id) is None:
        raise HTTPException(status_code=400, detail="Unknown user")
    fhir_user = body.fhir_user.strip()
    if await db.scalar(select(EhrUserLink.link_id).where(EhrUserLink.issuer_id == body.issuer_id,
                                                         EhrUserLink.fhir_user == fhir_user)):
        raise HTTPException(status_code=409, detail="This EHR user is already linked")
    link = EhrUserLink(issuer_id=body.issuer_id, fhir_user=fhir_user, user_id=body.user_id, created_by=user.user_id)
    db.add(link)
    await db.flush()
    await smart.audit_admin(db, user, "LINK_EHR_USER", link.link_id, f"user {body.user_id} issuer {body.issuer_id}",
                            request)
    return link.to_dict()


@router.delete("/links/{link_id}")
async def delete_link(link_id: str, request: Request, db: DB, user: CurrentUser = Depends(requires("user:manage"))):
    link = await db.get(EhrUserLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.delete(link)
    await smart.audit_admin(db, user, "UNLINK_EHR_USER", link_id, f"user {link.user_id}", request)
    return {"success": True}
