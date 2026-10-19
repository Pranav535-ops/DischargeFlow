"""Parking of out-of-order ADT messages (ADR-002 item 11, finding 10).

An event about an MRN DischargeFlow has not seen yet (typically an A03 that overtook its A01 in the interface engine)
is not refused: an AE there is usually dropped or stalls the queue. It is answered AA with a note and parked. When an
A01 (or an A40 re-key) brings the MRN in, the parked messages for it are replayed in arrival order, in the same
transaction. Parked messages that wait longer than HL7_PARK_HOURS expire unapplied, and admins are told.

The message itself is PHI, so it is stored only as ciphertext under a data key of its own (wrapped by the KEK), and
both are dropped as soon as the message is replayed, expires or is discarded. Admins see ids, events, times and
outcomes (GET /api/hl7/parked), never the content.
"""
from datetime import timedelta
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core import crypto
from app.core.config import settings
from app.core.db import utcnow
from app.core.ids import new_id
from app.core.security import CurrentUser
from app.interop.hl7 import Message, parse
from app.interop.models import HL7Message, HL7ParkedMessage
from app.notifications.service import notify_role
from app.patients.models import MRN_INDEX_PURPOSE, mrn_index_parts

# What the sender is told: AA, so its queue moves on, and why nothing visible happened yet.
PARKED_TEXT = "Parked: no patient with this MRN yet; it will be applied when the admission (A01) arrives"


def _aad(parked: HL7ParkedMessage) -> str:
    return f"{parked.parked_id}:message"


def _wipe(parked: HL7ParkedMessage, status: str) -> None:
    parked.status = status
    parked.resolved_at = utcnow()
    parked.message = None
    parked.dek = None


async def park(db: AsyncSession, client: CurrentUser, record: HL7Message, raw: str, authority: Optional[str],
               mrn: str) -> HL7ParkedMessage:
    now = utcnow()
    # The id is set here because it is the ciphertext's associated data.
    parked = HL7ParkedMessage(parked_id=new_id("hlp", 16), message_id=record.message_id, client_id=client.user_id,
                              event=record.message_type,
                              mrn_index=crypto.blind_index(MRN_INDEX_PURPOSE,
                                                           *mrn_index_parts(client.tenant_id, authority, mrn)),
                              received_at=now, expires_at=now + timedelta(hours=settings.hl7_park_hours))
    parked.dek = crypto.new_wrapped_dek()
    parked.message = crypto.encrypt_field(parked.dek, raw, _aad(parked))
    db.add(parked)
    await db.flush()
    return parked


async def expire_due(db: AsyncSession) -> int:
    """Expire parked messages past their time (run on every ADT request and admin listing); tell admins."""
    due = list(await db.scalars(select(HL7ParkedMessage).where(
        HL7ParkedMessage.status == "parked", HL7ParkedMessage.expires_at <= utcnow())))
    for parked in due:
        _wipe(parked, "expired")
        parked.result_text = "Expired: the admission for this MRN never arrived"
    if due:
        await notify_role(db, "admin", "Parked HL7 Messages Expired",
                          f"{len(due)} parked ADT message(s) expired without being applied. Check the interface "
                          f"engine for missing admissions.", "/admin")
    return len(due)


async def replay_for(db: AsyncSession, client: CurrentUser, authority: Optional[str], mrn: str,
                     apply: Callable[[Message], Awaitable[tuple[str, str, Optional[str], bool]]]) -> int:
    """Replay the messages parked for this MRN, oldest first. `apply` processes one message and returns
    (ack code, text, patient id, parked again); a message that would be parked again stays parked."""
    candidates = crypto.blind_index_candidates(MRN_INDEX_PURPOSE, *mrn_index_parts(client.tenant_id, authority, mrn))
    waiting = list(await db.scalars(select(HL7ParkedMessage).where(
        HL7ParkedMessage.status == "parked", HL7ParkedMessage.mrn_index.in_(candidates),
        HL7ParkedMessage.expires_at > utcnow()).order_by(HL7ParkedMessage.received_at)))
    replayed = 0
    for parked in waiting:
        assert parked.dek is not None and parked.message is not None
        message = parse(crypto.decrypt_field(parked.dek, parked.message, _aad(parked)))
        ack_code, text, patient_id, parked_again = await apply(message)
        if parked_again:
            continue
        _wipe(parked, "replayed")
        parked.result_ack, parked.result_text = ack_code, text[:500]
        original = await db.get(HL7Message, parked.message_id)
        if original is not None and patient_id:
            original.patient_id = patient_id
        await write_audit(db, client, "HL7_ADT_REPLAYED", "patient" if patient_id else "hl7_message",
                          patient_id or parked.message_id,
                          f"{parked.event} {parked.message_id} parked {parked.parked_id} → {ack_code}: {text}")
        replayed += 1
    return replayed


async def list_parked(db: AsyncSession, status: Optional[str] = None) -> list[dict]:
    await expire_due(db)
    stmt = select(HL7ParkedMessage).order_by(HL7ParkedMessage.received_at.desc()).limit(500)
    if status:
        stmt = stmt.where(HL7ParkedMessage.status == status)
    rows = list(await db.scalars(stmt))
    controls = dict((await db.execute(select(HL7Message.message_id, HL7Message.control_id).where(
        HL7Message.message_id.in_([p.message_id for p in rows])))).tuples().all()) if rows else {}
    return [{**p.to_dict(), "control_id": controls.get(p.message_id)} for p in rows]


async def discard(db: AsyncSession, parked: HL7ParkedMessage) -> None:
    _wipe(parked, "discarded")
    parked.result_text = "Discarded by an administrator"
