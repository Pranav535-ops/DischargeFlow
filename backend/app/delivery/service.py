"""Outbound delivery of the signed discharge summary (ADR-002 item 16), as a transactional outbox.

Signing a summary (or adding an addendum, or adding a recipient to a signed stay) inserts one `summary_deliveries`
row per recipient in the same transaction: if the signature rolls back, nothing is queued, and nothing is ever sent
for text that was not signed. Sending happens after commit:

- right after the signing response, best effort, for the rows that request queued (`kick`, a background task);
- by the retry runner (`run_tenant` / `run_all_tenants`): `python -m app.cli deliver-summaries`, Vercel Cron
  (`GET /api/deliveries/run` with CRON_SECRET), or `POST /api/deliveries/run` (integration key or admin).

The runner claims due rows with `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres), so two runs never send the same row
at once, and backs off between attempts (BACKOFF_MINUTES). A permanent refusal (4xx), an inactive endpoint or the
last attempt marks a delivery `failed`: it then waits on the worklist (`GET /api/deliveries?status=failed`) for a
case manager or admin to retry or cancel it, and nurses on the patient's unit and admins are notified. Delivery is
at least once: a run whose commit fails sends again, which the stable Idempotency-Key lets the receiver drop.

Erasure cancels a patient's open deliveries, and the runner re-checks before every send. Audit entries carry
delivery, endpoint and summary ids, the format and the outcome code; never document text.
"""
import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.models import DischargeSummary
from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.core.tenancy import current_tenant_id, use_tenant
from app.delivery import documents
from app.delivery.models import DeliveryEndpoint, DischargeRecipient, SummaryDelivery
from app.delivery.transport import Outgoing, Result, get_transport
from app.discharge.models import Workflow
from app.downtime.service import read_only_status
from app.identity.models import Tenant, User
from app.notifications.service import notify_role
from app.patients.models import Encounter, Patient

logger = logging.getLogger(__name__)

# Wait before attempt n+1 (index n-1). After the last, the delivery fails onto the worklist (~17 hours in all).
BACKOFF_MINUTES = (1, 5, 15, 60, 240, 720)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
MAX_PER_RUN = 25
# Stop starting sends after this long, so a run stays inside a serverless function's time limit.
RUN_BUDGET_SECONDS = 20.0
SYSTEM_ACTOR_ID = "system:delivery"
FAILED_TITLE = "Discharge summary delivery failed"


def system_actor(tenant_id: str) -> CurrentUser:
    return CurrentUser(user_id=SYSTEM_ACTOR_ID, email="", name="Scheduled summary delivery", role="system",
                       tenant_id=tenant_id, session_id=SYSTEM_ACTOR_ID, created_at=utcnow())


def delivery_view(delivery: SummaryDelivery, endpoint: Optional[DeliveryEndpoint] = None) -> dict:
    out = delivery.to_dict()
    if endpoint is not None:
        out["endpoint_name"] = endpoint.name
        out["endpoint_kind"] = endpoint.kind
    out["link"] = f"/workflows/{delivery.workflow_id}"
    return out


# ---------------------------------------------------------------- enqueue (inside the caller's transaction)

async def _encounter_id(db: AsyncSession, workflow: Workflow) -> Optional[str]:
    if workflow.encounter_id:
        return workflow.encounter_id
    return await db.scalar(select(Encounter.encounter_id).where(
        Encounter.patient_id == workflow.patient_id, Encounter.status == "active"))


def latest_trigger(summary: DischargeSummary) -> Optional[str]:
    """What a delivery made now should carry: the latest signed version and every addendum to it."""
    version = summary.latest_version
    if version is None:
        return None
    addenda = summary.addenda_for(version)
    return addenda[-1].addendum_id if addenda else version.version_id


async def _enqueue(db: AsyncSession, summary: DischargeSummary, workflow: Workflow, encounter_id: Optional[str],
                   endpoints: Iterable[DeliveryEndpoint], trigger_id: str) -> list[SummaryDelivery]:
    created = []
    for endpoint in endpoints:
        exists = await db.scalar(select(SummaryDelivery.delivery_id).where(
            SummaryDelivery.summary_id == summary.summary_id, SummaryDelivery.endpoint_id == endpoint.endpoint_id,
            SummaryDelivery.trigger_id == trigger_id))
        if exists:
            continue
        delivery = SummaryDelivery(summary_id=summary.summary_id, patient_id=summary.patient_id,
                                   workflow_id=workflow.workflow_id, encounter_id=encounter_id,
                                   endpoint_id=endpoint.endpoint_id, trigger_id=trigger_id,
                                   document_format=endpoint.document_format, status="queued",
                                   next_attempt_at=utcnow())
        db.add(delivery)
        created.append(delivery)
    await db.flush()
    return created


async def _recipient_endpoints(db: AsyncSession, encounter_id: Optional[str]) -> list[DeliveryEndpoint]:
    if encounter_id is None:
        return []
    return list(await db.scalars(
        select(DeliveryEndpoint).join(DischargeRecipient, DischargeRecipient.endpoint_id == DeliveryEndpoint.endpoint_id)
        .where(DischargeRecipient.encounter_id == encounter_id, DeliveryEndpoint.active.is_(True))
        .order_by(DischargeRecipient.added_at)))


async def enqueue_for_summary(db: AsyncSession, user: CurrentUser, summary: DischargeSummary,
                              trigger_id: Optional[str] = None) -> list[SummaryDelivery]:
    """Queue the signed summary (or its new addendum) for every recipient of the stay. Call in the transaction that
    signs it. Nothing is queued for an unsigned summary or an erased patient."""
    trigger_id = trigger_id or latest_trigger(summary)
    if not summary.approved or trigger_id is None or summary.patient.gdpr_erased:
        return []
    workflow = await db.get(Workflow, summary.workflow_id)
    if workflow is None:
        return []
    encounter_id = await _encounter_id(db, workflow)
    created = await _enqueue(db, summary, workflow, encounter_id, await _recipient_endpoints(db, encounter_id),
                             trigger_id)
    if created:
        await write_audit(db, user, "QUEUE_SUMMARY_DELIVERY", "discharge_summary", summary.summary_id,
                          f"trigger={trigger_id} deliveries={','.join(d.delivery_id for d in created)}")
    return created


async def enqueue_for_recipient(db: AsyncSession, user: CurrentUser, recipient: DischargeRecipient,
                                endpoint: DeliveryEndpoint) -> list[SummaryDelivery]:
    """A recipient added after signature gets the summary as it stands now (signed version and addenda)."""
    summary = await db.scalar(
        select(DischargeSummary).join(Workflow, Workflow.workflow_id == DischargeSummary.workflow_id)
        .where(Workflow.encounter_id == recipient.encounter_id, DischargeSummary.approved.is_(True))
        .order_by(DischargeSummary.approved_at.desc()).limit(1))
    if summary is None or summary.patient.gdpr_erased or not endpoint.active:
        return []
    trigger_id = latest_trigger(summary)
    workflow = await db.get(Workflow, summary.workflow_id)
    if trigger_id is None or workflow is None:
        return []
    created = await _enqueue(db, summary, workflow, recipient.encounter_id, [endpoint], trigger_id)
    if created:
        await write_audit(db, user, "QUEUE_SUMMARY_DELIVERY", "discharge_summary", summary.summary_id,
                          f"trigger={trigger_id} deliveries={','.join(d.delivery_id for d in created)}")
    return created


async def cancel_for_patient(db: AsyncSession, patient_id: str, reason: str = "patient_erased") -> int:
    """Erasure: nothing more leaves the hospital for this patient."""
    rows = list(await db.scalars(select(SummaryDelivery).where(
        SummaryDelivery.patient_id == patient_id, SummaryDelivery.status.in_(("queued", "failed")))))
    for delivery in rows:
        delivery.status, delivery.last_error, delivery.next_attempt_at = "cancelled", reason, None
    await db.flush()
    return len(rows)


# ---------------------------------------------------------------- recipients and endpoints

async def get_endpoint(db: AsyncSession, endpoint_id: str) -> DeliveryEndpoint:
    endpoint = await db.get(DeliveryEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Delivery endpoint not found")
    return endpoint


def check_endpoint(transport: str, url: Optional[str]) -> None:
    if transport == "mock":
        if settings.is_production:
            raise HTTPException(status_code=400, detail="The mock transport is for development and tests only")
        return
    if not url:
        raise HTTPException(status_code=400, detail="An https endpoint needs a URL")
    if settings.is_production and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Endpoint URLs must be https")
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Endpoint URLs must be http(s)")


async def recipients_for(db: AsyncSession, encounter_id: str) -> list[dict]:
    rows = await db.execute(
        select(DischargeRecipient, DeliveryEndpoint)
        .join(DeliveryEndpoint, DeliveryEndpoint.endpoint_id == DischargeRecipient.endpoint_id)
        .where(DischargeRecipient.encounter_id == encounter_id).order_by(DischargeRecipient.added_at))
    return [{**r.to_dict(), "endpoint_name": e.name, "endpoint_kind": e.kind, "document_format": e.document_format,
             "endpoint_active": e.active} for r, e in rows]


# ---------------------------------------------------------------- the runner

async def _names(db: AsyncSession, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    return {u.user_id: u.name for u in await db.scalars(select(User).where(User.user_id.in_(user_ids)))}


async def build_document(db: AsyncSession, delivery: SummaryDelivery, summary: DischargeSummary,
                         endpoint: DeliveryEndpoint) -> Optional[documents.SummaryDocument]:
    """The document for one delivery: the signed version, plus addenda up to the trigger. None if not signed."""
    version = summary.latest_version
    if version is None:
        return None
    addenda = summary.addenda_for(version)
    if delivery.trigger_id != version.version_id:
        ids = [a.addendum_id for a in addenda]
        addenda = addenda[:ids.index(delivery.trigger_id) + 1] if delivery.trigger_id in ids else addenda
    else:
        addenda = []
    names = await _names(db, {version.signed_by, *(a.signed_by for a in addenda)})
    patient: Patient = summary.patient
    encounter = await db.get(Encounter, delivery.encounter_id) if delivery.encounter_id else None
    tenant = await db.get(Tenant, delivery.tenant_id)
    return documents.SummaryDocument(
        delivery_id=delivery.delivery_id, summary_id=summary.summary_id, version_id=version.version_id,
        version=version.version, signed_text=version.content, signed_at=version.signed_at,
        signer_id=version.signed_by, signer_name=names.get(version.signed_by, "Signing physician"),
        patient_id=patient.patient_id, patient_name=patient.name, birth_date=patient.date_of_birth,
        mrn=patient.mrn, mrn_authority=patient.mrn_authority, diagnosis=patient.diagnosis,
        encounter_id=delivery.encounter_id, visit_number=encounter.visit_number if encounter else None,
        admitted_at=encounter.admitted_at if encounter else None,
        discharged_at=encounter.discharged_at if encounter else None,
        disposition=encounter.disposition if encounter else None,
        tenant_id=delivery.tenant_id, organization_name=tenant.name if tenant else delivery.tenant_id,
        recipient_name=endpoint.name,
        addenda=[documents.Addendum(a.addendum_id, a.content, names.get(a.signed_by, "Physician"), a.signed_at)
                 for a in addenda],
    )


async def _give_up(db: AsyncSession, delivery: SummaryDelivery, error: str, unit_id: Optional[str]) -> None:
    delivery.status, delivery.last_error, delivery.next_attempt_at = "failed", error, None
    message = (f"Delivery {delivery.delivery_id} of a signed discharge summary could not be completed ({error}). "
               "Check the endpoint and retry or cancel it from the delivery worklist.")
    for role in ("nurse", "admin"):
        await notify_role(db, role, FAILED_TITLE, message, "/deliveries", unit_id=unit_id if role == "nurse" else None,
                          type_="error")


async def _record(db: AsyncSession, actor: CurrentUser, delivery: SummaryDelivery, outcome: str) -> None:
    await write_audit(db, actor, "DELIVER_SUMMARY", "discharge_summary", delivery.summary_id,
                      f"delivery={delivery.delivery_id} endpoint={delivery.endpoint_id} "
                      f"format={delivery.document_format} {outcome} attempt={delivery.attempts}"
                      + (f" http={delivery.last_http_status}" if delivery.last_http_status else ""))


async def deliver_one(db: AsyncSession, actor: CurrentUser, delivery: SummaryDelivery,
                      now: Optional[datetime] = None) -> str:
    """One attempt at one claimed delivery. Returns the outcome (acknowledged, sent, retry, failed, cancelled)."""
    now = now or utcnow()
    summary = await db.get(DischargeSummary, delivery.summary_id)
    endpoint = await db.get(DeliveryEndpoint, delivery.endpoint_id)
    if summary is None or summary.patient.gdpr_erased:
        delivery.status, delivery.last_error, delivery.next_attempt_at = "cancelled", "patient_erased", None
        await _record(db, actor, delivery, "cancelled reason=patient_erased")
        return "cancelled"
    unit_id = summary.patient.unit_id
    if endpoint is None or not endpoint.active:
        await _give_up(db, delivery, "endpoint_inactive", unit_id)
        await _record(db, actor, delivery, "failed reason=endpoint_inactive")
        return "failed"
    if endpoint.transport == "mock" and settings.is_production:
        await _give_up(db, delivery, "mock_in_production", unit_id)
        await _record(db, actor, delivery, "failed reason=mock_in_production")
        return "failed"
    doc = await build_document(db, delivery, summary, endpoint)
    if doc is None:
        await _give_up(db, delivery, "not_signed", unit_id)
        await _record(db, actor, delivery, "failed reason=not_signed")
        return "failed"
    body = documents.render(doc, delivery.document_format)
    digest = hashlib.sha256(body).hexdigest()
    outgoing = Outgoing(delivery_id=delivery.delivery_id, url=endpoint.url, document_format=delivery.document_format,
                        content_type=documents.CONTENT_TYPES[delivery.document_format], body=body,
                        idempotency_key=f"{delivery.tenant_id}:{delivery.delivery_id}:{digest[:16]}")
    try:
        result = await get_transport(endpoint.transport).send(outgoing)
    except Exception:  # an adapter must not raise; if one does, count it as a retryable failure
        logger.exception("Summary delivery transport raised", extra={"delivery_id": delivery.delivery_id})
        result = Result(ok=False, error="transport_error")
    delivery.attempts += 1
    delivery.last_attempt_at = now
    delivery.document_sha256 = digest
    delivery.last_http_status = result.http_status
    if result.ok:
        delivery.status = "acknowledged" if result.acknowledged else "sent"
        delivery.sent_at = now
        delivery.acknowledged_at = now if result.acknowledged else None
        delivery.remote_id = result.remote_id
        delivery.last_error, delivery.next_attempt_at = None, None
        await _record(db, actor, delivery, delivery.status)
        return delivery.status
    error = result.error or "error"
    if not result.retryable or delivery.attempts >= MAX_ATTEMPTS:
        await _give_up(db, delivery, error, unit_id)
        await _record(db, actor, delivery, f"failed reason={error}")
        return "failed"
    delivery.last_error = error
    delivery.next_attempt_at = now + timedelta(minutes=BACKOFF_MINUTES[delivery.attempts - 1])
    await _record(db, actor, delivery, f"retry reason={error}")
    return "retry"


async def run_tenant(db: AsyncSession, actor: CurrentUser, now: Optional[datetime] = None,
                     delivery_ids: Optional[list[str]] = None, limit: int = MAX_PER_RUN) -> dict:
    """Attempt the due deliveries of one tenant in the caller's transaction (already bound to the tenant).

    A read-only tenant (or a deployment forced read-only) is skipped: nothing is claimed, sent or updated, and its
    due rows stay queued for the first run after the downtime ends. The cron triggers get past the router-wide
    read-only check (a GET, or a cron secret that is not an access token), so this is where it is enforced."""
    now = now or utcnow()
    result: dict = {"tenant_id": actor.tenant_id, "claimed": 0, "acknowledged": 0, "sent": 0,
                    "retry": 0, "failed": 0, "cancelled": 0, "deferred": 0, "skipped": None}
    if (await read_only_status(db, actor.tenant_id))["read_only"]:
        result["skipped"] = "read_only"
        return result
    stmt = (select(SummaryDelivery)
            .where(SummaryDelivery.tenant_id == actor.tenant_id, SummaryDelivery.status == "queued",
                   SummaryDelivery.next_attempt_at <= now)
            .order_by(SummaryDelivery.next_attempt_at).limit(limit)
            # Rows another run holds are skipped, not waited for (ignored on SQLite, which runs one at a time).
            .with_for_update(skip_locked=True))
    if delivery_ids is not None:
        stmt = stmt.where(SummaryDelivery.delivery_id.in_(delivery_ids))
    claimed = list(await db.scalars(stmt))
    result["claimed"] = len(claimed)
    started = time.monotonic()
    for delivery in claimed:
        if time.monotonic() - started > RUN_BUDGET_SECONDS:
            result["deferred"] += 1  # still queued and due: the next run takes it
            continue
        result[await deliver_one(db, actor, delivery, now)] += 1
    await db.flush()
    return result


async def run_all_tenants(factory: async_sessionmaker[AsyncSession], now: Optional[datetime] = None) -> dict:
    """Every tenant, each in its own transaction, so one tenant's failure doesn't undo the others."""
    async with factory() as session:
        tenant_ids = list(await session.scalars(select(Tenant.tenant_id).order_by(Tenant.tenant_id)))
    results = []
    for tenant_id in tenant_ids:
        token = current_tenant_id.set(tenant_id)
        try:
            async with factory() as session:
                try:
                    await use_tenant(session, tenant_id)
                    results.append(await run_tenant(session, system_actor(tenant_id), now))
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception("Summary delivery run failed", extra={"tenant_id": tenant_id})
                    results.append({"tenant_id": tenant_id, "error": True})
        finally:
            current_tenant_id.reset(token)
    return {"tenants": results}


async def kick(factory: async_sessionmaker[AsyncSession], tenant_id: str, delivery_ids: list[str]) -> None:
    """First attempt right after the request that queued these rows has committed (a background task). Best
    effort: a failure here leaves them queued for the runner."""
    token = current_tenant_id.set(tenant_id)
    try:
        async with factory() as session:
            try:
                await use_tenant(session, tenant_id)
                await run_tenant(session, system_actor(tenant_id), delivery_ids=delivery_ids)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Immediate summary delivery failed", extra={"tenant_id": tenant_id})
    finally:
        current_tenant_id.reset(token)


def schedule(background_tasks, factory: async_sessionmaker[AsyncSession], tenant_id: str,
             deliveries: list[SummaryDelivery]) -> None:
    """Attach the first attempt to the response (runs after the request transaction commits)."""
    if deliveries:
        background_tasks.add_task(kick, factory, tenant_id, [d.delivery_id for d in deliveries])


# ---------------------------------------------------------------- worklist actions

async def get_delivery(db: AsyncSession, delivery_id: str) -> SummaryDelivery:
    delivery = await db.get(SummaryDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


async def retry(db: AsyncSession, delivery: SummaryDelivery) -> None:
    if delivery.status != "failed":
        raise HTTPException(status_code=409, detail="Only a failed delivery can be retried")
    patient = await db.get(Patient, delivery.patient_id)
    if patient is None or patient.gdpr_erased:
        raise HTTPException(status_code=409, detail="The patient's record was erased")
    # A fresh series of attempts (the earlier ones are in the audit log).
    delivery.status, delivery.next_attempt_at, delivery.last_error, delivery.attempts = "queued", utcnow(), None, 0
    await db.flush()


def cancel(delivery: SummaryDelivery) -> None:
    if delivery.status not in ("queued", "failed"):
        raise HTTPException(status_code=409, detail="Only a queued or failed delivery can be cancelled")
    delivery.status, delivery.next_attempt_at = "cancelled", None


def acknowledge(delivery: SummaryDelivery, remote_id: Optional[str]) -> None:
    if delivery.status not in ("sent", "acknowledged"):
        raise HTTPException(status_code=409, detail="Only a sent delivery can be acknowledged")
    if delivery.status == "sent":
        delivery.status, delivery.acknowledged_at = "acknowledged", utcnow()
    if remote_id:
        delivery.remote_id = remote_id[:300]

