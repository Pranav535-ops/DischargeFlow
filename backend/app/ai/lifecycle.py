"""Discharge summary lifecycle (ADR-002 item 13): signed versions, addenda, and the sign-by deadline.

Signing (the physician's approval) freezes the text as a DischargeSummaryVersion with a SHA-256 of what was signed.
From then on the summary's text can't change; anything added is an addendum, signed by its author as it is added.
Hospital policy sets how long after discharge the summary must be signed (SUMMARY_SIGN_HOURS).
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import DischargeSummary, DischargeSummaryAddendum, DischargeSummaryVersion, content_sha256
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import Workflow
from app.notifications.models import Notification
from app.notifications.service import notify_role
from app.patients.models import Patient

OVERDUE_TITLE = "Discharge Summary Overdue"
MAX_ADDENDUM_CHARS = 20000


def sign_by(workflow: Workflow) -> datetime:
    """When the summary must be signed: policy hours after the patient left.

    Before the patient has left, the expected discharge time stands in for it (and the start of the discharge when
    there is none), so a discharge that stalls unsigned still becomes overdue.
    """
    anchor = workflow.completed_at or workflow.estimated_discharge_time or workflow.started_at
    return anchor + timedelta(hours=settings.summary_sign_hours)


def _version_dict(v: DischargeSummaryVersion, with_content: bool) -> dict:
    out = {k: v.to_dict()[k] for k in ("version_id", "version", "signed_by", "signed_at", "content_sha256")}
    if with_content:
        out["content"] = v.content
        # Whether the text decrypts to what was signed (None once erased, or for a legacy version without a hash).
        out["hash_verified"] = None if v.content_sha256 is None else content_sha256(v.content) == v.content_sha256
    return out


def addendum_dict(a: DischargeSummaryAddendum) -> dict:
    d = a.to_dict()
    return {k: d[k] for k in ("addendum_id", "version_id", "signed_by", "signed_at", "content", "content_sha256")}


def summary_view(summary: DischargeSummary, workflow: Optional[Workflow] = None) -> dict:
    """The summary as the API returns it: the current text, its signed state, the version history (metadata only),
    and the latest signed version with its text and its addenda."""
    out = summary.to_dict()
    latest = summary.latest_version
    out["signed"] = bool(summary.approved)
    out["versions"] = [_version_dict(v, with_content=False) for v in summary.versions]
    out["signed_version"] = _version_dict(latest, with_content=True) if latest else None
    out["addenda"] = [addendum_dict(a) for a in summary.addenda_for(latest)]
    if workflow is not None:
        deadline = sign_by(workflow)
        out["sign_by"] = deadline.isoformat()
        out["signed_late"] = bool(summary.approved_at and summary.approved_at > deadline)
    return out


def sign(summary: DischargeSummary, user: CurrentUser) -> DischargeSummaryVersion:
    """Record the signature of an approved summary (ai.service.apply_approval has set approved_*)."""
    return summary.record_signed_version(user.user_id, summary.approved_at or utcnow())


def add_addendum(summary: DischargeSummary, user: CurrentUser, text: str) -> DischargeSummaryAddendum:
    """Append signed text to the latest signed version. The summary and earlier addenda stay as they are."""
    if not summary.approved:
        raise HTTPException(status_code=400, detail="Only a signed summary takes addenda; edit the draft instead")
    if summary.patient.gdpr_erased:
        raise HTTPException(status_code=400, detail="The patient's record was erased")
    text = text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Addendum text is required")
    version = summary.latest_version
    if version is None:  # approved before migration 0013 and not yet backfilled by `app.cli migrate`
        version = summary.record_signed_version(summary.approved_by or user.user_id, summary.approved_at or utcnow())
    addendum = DischargeSummaryAddendum(
        patient=summary.patient, patient_id=summary.patient_id, summary_id=summary.summary_id,
        version_id=version.version_id, content=text, content_sha256=content_sha256(text),
        signed_by=user.user_id, signed_at=utcnow(),
    )
    summary.addenda.append(addendum)
    return addendum


async def unsigned_overdue(db: AsyncSession, now: Optional[datetime] = None) -> list[dict]:
    """Discharges whose summary is missing or unsigned past the sign-by deadline, most overdue first.
    Ids and times only, no PHI. Row-level security limits it to what the caller may see."""
    now = now or utcnow()
    rows = await db.execute(
        select(Workflow, DischargeSummary.summary_id, Patient.unit_id)
        .join(Patient, Patient.patient_id == Workflow.patient_id)
        .outerjoin(DischargeSummary, DischargeSummary.workflow_id == Workflow.workflow_id)
        .where((DischargeSummary.summary_id.is_(None)) | (DischargeSummary.approved.is_(False)))
    )
    out = []
    for workflow, summary_id, unit_id in rows:
        deadline = sign_by(workflow)
        if deadline < now:
            out.append({"workflow_id": workflow.workflow_id, "patient_id": workflow.patient_id, "unit_id": unit_id,
                        "summary_id": summary_id, "workflow_status": workflow.status,
                        "sign_by": deadline.isoformat(),
                        "hours_overdue": round((now - deadline).total_seconds() / 3600, 1)})
    return sorted(out, key=lambda r: -r["hours_overdue"])


async def notify_overdue(db: AsyncSession, now: Optional[datetime] = None) -> int:
    """Tell physicians (on the patient's unit) about each overdue summary, once per discharge. Idempotent: a
    discharge whose overdue notice already exists is skipped. Returns how many discharges were notified."""
    notified = 0
    for row in await unsigned_overdue(db, now):
        link = f"/workflows/{row['workflow_id']}"
        exists = await db.scalar(select(Notification.notification_id).where(
            Notification.title == OVERDUE_TITLE, Notification.link == link).limit(1))
        if exists:
            continue
        await notify_role(db, "physician", OVERDUE_TITLE,
                          f"A discharge summary is unsigned past the {settings.summary_sign_hours:g}-hour sign-by "
                          "deadline. Review and sign it.", link, unit_id=row["unit_id"])
        notified += 1
    await db.flush()
    return notified
