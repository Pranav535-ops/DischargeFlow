import hmac
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import DB, get_session_factory
from app.core.security import AuthUser, CurrentUser, ensure_permission, get_current_user, get_signed_in_identity, requires
from app.delivery import service
from app.delivery.models import DELIVERY_STATUSES, DeliveryEndpoint, DischargeRecipient, SummaryDelivery
from app.identity.integrations import authenticate, requires_integration
from app.patients.models import Encounter

endpoints_router = APIRouter(prefix="/delivery-endpoints", tags=["Summary delivery"])
recipients_router = APIRouter(prefix="/encounters", tags=["Summary delivery"])
deliveries_router = APIRouter(prefix="/deliveries", tags=["Summary delivery"])
routers = (endpoints_router, recipients_router, deliveries_router)

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
RUN_PERMISSION = "delivery:run"


class EndpointIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["primary_care", "receiving_facility", "other"] = "primary_care"
    document_format: Literal["fhir_bundle", "ccda"] = "fhir_bundle"
    transport: Literal["https", "mock"] = "https"
    url: Optional[str] = Field(default=None, max_length=2048)
    active: bool = True


class EndpointUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    kind: Optional[Literal["primary_care", "receiving_facility", "other"]] = None
    document_format: Optional[Literal["fhir_bundle", "ccda"]] = None
    transport: Optional[Literal["https", "mock"]] = None
    url: Optional[str] = Field(default=None, max_length=2048)
    active: Optional[bool] = None


class RecipientIn(BaseModel):
    endpoint_id: str


class AcknowledgeIn(BaseModel):
    remote_id: Optional[str] = Field(default=None, max_length=300)


# ---------------------------------------------------------------- endpoint directory (admin-managed)

@endpoints_router.get("")
async def list_endpoints(db: DB, user: AuthUser, include_inactive: bool = False):
    """The receivers a stay's summary can be sent to. Readable by every signed-in user, to pick recipients."""
    stmt = select(DeliveryEndpoint).order_by(DeliveryEndpoint.name)
    if not include_inactive:
        stmt = stmt.where(DeliveryEndpoint.active.is_(True))
    return [e.to_dict() for e in await db.scalars(stmt)]


@endpoints_router.post("", status_code=201)
async def create_endpoint(body: EndpointIn, request: Request, db: DB,
                          user: CurrentUser = Depends(requires("delivery:configure"))):
    url = (body.url or "").strip() or None
    service.check_endpoint(body.transport, url)
    endpoint = DeliveryEndpoint(name=body.name.strip(), kind=body.kind, document_format=body.document_format,
                                transport=body.transport, url=url, active=body.active, created_by=user.user_id)
    db.add(endpoint)
    await db.flush()
    await write_audit(db, user, "CREATE_DELIVERY_ENDPOINT", "delivery_endpoint", endpoint.endpoint_id,
                      f"format={endpoint.document_format} transport={endpoint.transport}", request)
    return endpoint.to_dict()


@endpoints_router.put("/{endpoint_id}")
async def update_endpoint(endpoint_id: str, body: EndpointUpdate, request: Request, db: DB,
                          user: CurrentUser = Depends(requires("delivery:configure"))):
    endpoint = await service.get_endpoint(db, endpoint_id)
    changes = body.model_dump(exclude_unset=True)
    if "url" in changes:
        changes["url"] = (changes["url"] or "").strip() or None
    service.check_endpoint(changes.get("transport", endpoint.transport), changes.get("url", endpoint.url))
    for field, value in changes.items():
        if value is not None or field == "url":
            setattr(endpoint, field, value)
    await db.flush()
    await write_audit(db, user, "UPDATE_DELIVERY_ENDPOINT", "delivery_endpoint", endpoint_id,
                      f"fields: {', '.join(sorted(changes)) or 'none'}", request)
    return endpoint.to_dict()


# ---------------------------------------------------------------- recipients of one stay

async def _encounter(db: DB, encounter_id: str) -> Encounter:
    encounter = await db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return encounter


@recipients_router.get("/{encounter_id}/recipients")
async def list_recipients(encounter_id: str, db: DB, user: AuthUser):
    await _encounter(db, encounter_id)
    return await service.recipients_for(db, encounter_id)


@recipients_router.post("/{encounter_id}/recipients", status_code=201)
async def add_recipient(encounter_id: str, body: RecipientIn, request: Request, db: DB,
                        background_tasks: BackgroundTasks, factory: SessionFactory,
                        user: CurrentUser = Depends(requires("plan:manage"))):
    """Send this stay's discharge summary to a receiver. If the summary is already signed, it is queued now."""
    encounter = await _encounter(db, encounter_id)
    endpoint = await service.get_endpoint(db, body.endpoint_id)
    if not endpoint.active:
        raise HTTPException(status_code=400, detail="That endpoint is inactive")
    if await db.scalar(select(DischargeRecipient.recipient_id).where(
            DischargeRecipient.encounter_id == encounter_id, DischargeRecipient.endpoint_id == endpoint.endpoint_id)):
        raise HTTPException(status_code=409, detail="Already a recipient of this stay's summary")
    recipient = DischargeRecipient(encounter_id=encounter_id, patient_id=encounter.patient_id,
                                   endpoint_id=endpoint.endpoint_id, added_by=user.user_id)
    db.add(recipient)
    await db.flush()
    await write_audit(db, user, "ADD_SUMMARY_RECIPIENT", "patient", encounter.patient_id,
                      f"encounter={encounter_id} endpoint={endpoint.endpoint_id}", request)
    queued = await service.enqueue_for_recipient(db, user, recipient, endpoint)
    service.schedule(background_tasks, factory, user.tenant_id, queued)
    return {**recipient.to_dict(), "queued": [d.delivery_id for d in queued]}


@recipients_router.delete("/{encounter_id}/recipients/{recipient_id}")
async def remove_recipient(encounter_id: str, recipient_id: str, request: Request, db: DB,
                           user: CurrentUser = Depends(requires("plan:manage"))):
    """Stop sending future documents (addenda) to this receiver. Deliveries already made stay on record."""
    recipient = await db.get(DischargeRecipient, recipient_id)
    if recipient is None or recipient.encounter_id != encounter_id:
        raise HTTPException(status_code=404, detail="Recipient not found")
    await db.delete(recipient)
    await write_audit(db, user, "REMOVE_SUMMARY_RECIPIENT", "patient", recipient.patient_id,
                      f"encounter={encounter_id} endpoint={recipient.endpoint_id}", request)
    return {"success": True}


# ---------------------------------------------------------------- deliveries: worklist and runner

def _is_cron(request: Request) -> bool:
    """Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`. Off (never matches) until CRON_SECRET is set."""
    if not settings.cron_secret:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {settings.cron_secret}".encode())


@deliveries_router.get("/run")
async def run_from_cron(request: Request, factory: SessionFactory):
    """Vercel Cron's call (cron jobs send GET). Runs every tenant, each in its own transaction."""
    if not _is_cron(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await service.run_all_tenants(factory)


@deliveries_router.post("/run")
async def run_deliveries(request: Request, db: DB, factory: SessionFactory,
                         x_api_key: Annotated[Optional[str], Header()] = None):
    """Attempt every due delivery now. The cron secret runs every tenant; an integration client's API key, or a
    signed-in user, with `delivery:run` runs their own tenant."""
    if _is_cron(request):
        return await service.run_all_tenants(factory)
    actor: CurrentUser
    if x_api_key:
        actor = await authenticate(db, x_api_key)
    else:
        actor = await get_current_user(await get_signed_in_identity(request, db))
    await ensure_permission(db, actor, RUN_PERMISSION)
    return {"tenants": [await service.run_tenant(db, actor)]}


@deliveries_router.get("")
async def list_deliveries(db: DB, user: AuthUser, status: Optional[str] = None,
                          workflow_id: Optional[str] = None, limit: int = Query(200, ge=1, le=500)):
    """Deliveries, newest first, with ids, receiver, state and error codes (no PHI). The worklist is
    `?status=failed` (`delivery:manage`); one discharge's deliveries (`?workflow_id=`) are visible to everyone
    who can see the patient."""
    if workflow_id is None:
        await ensure_permission(db, user, "delivery:manage")
    if status is not None and status not in DELIVERY_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(DELIVERY_STATUSES)}")
    stmt = (select(SummaryDelivery, DeliveryEndpoint)
            .join(DeliveryEndpoint, DeliveryEndpoint.endpoint_id == SummaryDelivery.endpoint_id)
            .order_by(SummaryDelivery.created_at.desc()).limit(limit))
    if status:
        stmt = stmt.where(SummaryDelivery.status == status)
    if workflow_id:
        stmt = stmt.where(SummaryDelivery.workflow_id == workflow_id)
    return [service.delivery_view(d, e) for d, e in await db.execute(stmt)]


@deliveries_router.post("/{delivery_id}/retry")
async def retry_delivery(delivery_id: str, request: Request, db: DB, background_tasks: BackgroundTasks,
                         factory: SessionFactory, user: CurrentUser = Depends(requires("delivery:manage"))):
    delivery = await service.get_delivery(db, delivery_id)
    await service.retry(db, delivery)
    await write_audit(db, user, "RETRY_SUMMARY_DELIVERY", "discharge_summary", delivery.summary_id,
                      f"delivery={delivery_id}", request)
    service.schedule(background_tasks, factory, user.tenant_id, [delivery])
    return service.delivery_view(delivery)


@deliveries_router.post("/{delivery_id}/cancel")
async def cancel_delivery(delivery_id: str, request: Request, db: DB,
                          user: CurrentUser = Depends(requires("delivery:manage"))):
    delivery = await service.get_delivery(db, delivery_id)
    service.cancel(delivery)
    await write_audit(db, user, "CANCEL_SUMMARY_DELIVERY", "discharge_summary", delivery.summary_id,
                      f"delivery={delivery_id}", request)
    return service.delivery_view(delivery)


@deliveries_router.post("/{delivery_id}/acknowledge")
async def acknowledge_delivery(delivery_id: str, request: Request, db: DB, body: Optional[AcknowledgeIn] = None,
                               client: CurrentUser = Depends(requires_integration("delivery:acknowledge"))):
    """A receiver (or the interface engine in front of it) confirms it stored a document it accepted with 202."""
    delivery = await service.get_delivery(db, delivery_id)
    service.acknowledge(delivery, body.remote_id if body else None)
    await write_audit(db, client, "ACKNOWLEDGE_SUMMARY_DELIVERY", "discharge_summary", delivery.summary_id,
                      f"delivery={delivery_id}", request)
    return service.delivery_view(delivery)
