"""POST /api/hl7/adt: inbound HL7 v2 ADT messages from an integration client (see interop/adt.py), and the admin
view of parked out-of-order messages (interop/parking.py).

The body is one ER7 message; the response is its ACK in ER7, with HTTP 200 for every outcome the ACK can express
(AA, AE, AR). HTTP errors are reserved for problems below HL7: a missing or invalid API key (401), a client without
`adt:ingest` (403), a body over the size limit (413) and a concurrent duplicate (409, retry).
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit.service import write_audit
from app.core.db import DB
from app.core.security import CurrentUser, requires
from app.identity.integrations import requires_integration
from app.interop import adt, parking
from app.interop.hl7 import HL7Error, build_ack, parse
from app.interop.models import HL7Message, HL7ParkedMessage

router = APIRouter(prefix="/hl7", tags=["HL7 v2"])

HL7_MEDIA_TYPE = "x-application/hl7-v2+er7"
MAX_MESSAGE_BYTES = 256 * 1024


def _ack_response(ack: str) -> Response:
    return Response(content=ack, media_type=HL7_MEDIA_TYPE)


@router.post("/adt", response_class=Response)
async def receive_adt(request: Request, db: DB, client: CurrentUser = Depends(requires_integration("adt:ingest"))):
    body = await request.body()
    if len(body) > MAX_MESSAGE_BYTES:
        raise HTTPException(status_code=413, detail="HL7 message too large")
    raw = body.decode("utf-8", errors="replace")

    try:
        message = parse(raw)
    except HL7Error as exc:
        await write_audit(db, client, "HL7_ADT_REJECTED", "hl7_message", None, f"AR {exc.code}: {exc.text}", request)
        return _ack_response(build_ack(None, "AR", exc.text, exc.code))

    control_id = message.control_id
    event = f"{message.message_type}^{message.trigger_event}"
    if not control_id or len(control_id) > 199:
        text = "MSH-10 message control id is required (at most 199 characters)"
        await write_audit(db, client, "HL7_ADT_REJECTED", "hl7_message", None, f"AR 101: {text}", request)
        return _ack_response(build_ack(message, "AR", text, "101"))

    previous = await db.scalar(select(HL7Message).where(
        HL7Message.client_id == client.user_id, HL7Message.control_id == control_id))
    if previous is not None:
        # A retry (for example after a lost ACK): answer as before and change nothing.
        await write_audit(db, client, "HL7_ADT_REPLAY", "hl7_message", previous.message_id,
                          f"{previous.message_type} resent; original ACK {previous.ack_code} returned", request)
        return _ack_response(build_ack(message, previous.ack_code, previous.ack_text or "", previous.error_code))

    record = HL7Message(client_id=client.user_id, control_id=control_id, message_type=event[:20], ack_code="AR")
    db.add(record)
    try:
        await db.flush()  # claims the control id before anything is applied
    except IntegrityError:
        raise HTTPException(status_code=409, detail="This message is already being processed; retry")

    outcome = await adt.receive(db, client, message, raw, record)
    record.ack_code, record.error_code = outcome.ack_code, outcome.error_code
    record.ack_text, record.patient_id = outcome.text[:500], outcome.patient_id

    resource_type, resource_id = (("patient", outcome.patient_id) if outcome.patient_id
                                  else ("hl7_message", record.message_id))
    # AR means the message was never applied (wrong type, event or processing id), whatever its trigger says.
    action = ("HL7_ADT_REJECTED" if outcome.ack_code == "AR" else
              "HL7_ADT_PARKED" if outcome.park is not None else f"HL7_ADT_{message.trigger_event}")
    code = " ".join(c for c in (outcome.ack_code, outcome.error_code) if c)
    details = f"{record.message_type} {record.message_id} → {code}: {outcome.text}"
    await write_audit(db, client, action, resource_type, resource_id, details, request)
    return _ack_response(build_ack(message, outcome.ack_code, outcome.text, outcome.error_code))


@router.get("/parked")
async def list_parked(db: DB, _: CurrentUser = Depends(requires("user:manage")),
                      status: Optional[Literal["parked", "replayed", "expired", "discarded"]] = None):
    """Parked out-of-order messages, newest first: ids, event, times and outcome, never the message."""
    return await parking.list_parked(db, status)


@router.post("/parked/{parked_id}/discard")
async def discard_parked(parked_id: str, request: Request, db: DB,
                         user: CurrentUser = Depends(requires("user:manage"))):
    parked = await db.get(HL7ParkedMessage, parked_id)
    if parked is None:
        raise HTTPException(status_code=404, detail="Parked message not found")
    if parked.status != "parked":
        raise HTTPException(status_code=400, detail=f"Parked message is already {parked.status}")
    await parking.discard(db, parked)
    await write_audit(db, user, "HL7_ADT_DISCARDED", "hl7_message", parked.message_id,
                      f"{parked.event} parked {parked_id} discarded unapplied", request)
    return parked.to_dict()
