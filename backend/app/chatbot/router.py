"""Keyword-based assistant. Phase 3 replaces intent matching with LLM tool calling over the service layer."""
import re

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.audit.service import write_audit
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser
from app.discharge import service as discharge_service
from app.discharge.models import Task
from app.discharge.router import visible_tasks_for
from app.insurance.models import InsuranceClaim
from app.patients.models import Patient

router = APIRouter(prefix="/chatbot", tags=["Chatbot"])


class ChatRequest(BaseModel):
    message: str


# Order matters: the first matching intent wins, so more specific phrases come first.
INTENTS = [
    ("create_patient", ["create patient", "add patient", "new patient"]),
    ("list_patients", ["list patients", "show patients", "all patients"]),
    ("list_tasks", ["my tasks", "list tasks", "show tasks"]),
    ("create_task", ["create task", "add task", "new task", "assign task", "create a task"]),
    ("help", ["help", "what can you do", "commands"]),
    ("analytics", ["analytics", "stats", "statistics", "dashboard stats"]),
    ("submit_claim", ["submit claim", "new claim", "file claim", "submit new claim"]),
    ("list_claims", ["claims", "insurance", "billing"]),
]

HELP_TEXT = (
    "I can help you with the following:\n"
    "• **List patients** — show all current patients\n"
    "• **My tasks** — view your assigned tasks\n"
    "• **Analytics** — get a quick system snapshot\n"
    "• **Insurance/Claims** — check insurance claim status\n"
    "• **Create task** — e.g. 'create a task for patient 1 to give meds'\n"
    "• **Create patient** — guide you to admission form"
)
PATIENT_FORMAT_HINT = (
    "Please provide patient details in a structured format:\n"
    "Patient Name: John Doe\n"
    "DOB: 12/05/1980\n"
    "Admission Date: 20/01/2026\n"
    "Room: 101A\n"
    "Diagnosis: Pneumonia\n"
    "Physician: Dr. Smith"
)


def parse_intent(message: str) -> str:
    msg = message.lower().strip()
    for intent, phrases in INTENTS:
        if any(p in msg for p in phrases):
            return intent
    return "unknown"


def _value_after_colon(line: str) -> str:
    return line.split(":", 1)[1].strip()


def parse_patient_fields(message: str) -> dict:
    """Read 'Label: value' lines (or a label line followed by its value on the next line)."""
    fields = dict.fromkeys(["name", "date_of_birth", "admission_date", "room_number", "diagnosis", "attending_physician"], "")
    pending_key = None
    for line in (l.strip() for l in message.split("\n") if l.strip()):
        lower = line.lower()
        if "name" in lower and "patient" in lower:
            key = "name"
        elif "date of birth" in lower or "dob" in lower:
            key = "date_of_birth"
        elif "admission" in lower:
            key = "admission_date"
        elif "room" in lower:
            key = "room_number"
        elif "diagnosis" in lower:
            key = "diagnosis"
        elif "physician" in lower or "doctor" in lower:
            key = "attending_physician"
        else:
            if pending_key:
                fields[pending_key] = line
                pending_key = None
            continue
        if ":" in line:
            fields[key] = _value_after_colon(line)
            pending_key = None
        else:
            pending_key = key
    return fields


def parse_claim_fields(message: str) -> dict:
    fields = dict.fromkeys(["patient", "insurer_name", "policy_number", "billed_amount", "diagnosis_codes", "procedure_codes"], "")
    pending_key = None
    for line in (l.strip() for l in message.split("\n") if l.strip()):
        lower = line.lower()
        if "patient" in lower and "select" not in lower:
            key = "patient"
        elif "insurer name" in lower:
            key = "insurer_name"
        elif "policy number" in lower:
            key = "policy_number"
        elif "billed amount" in lower:
            key = "billed_amount"
        elif "diagnosis codes" in lower or "icd" in lower:
            key = "diagnosis_codes"
        elif "procedure codes" in lower or "cpt" in lower:
            key = "procedure_codes"
        else:
            # Skip template hint lines ("e.g. ...", "Select ...", "*").
            if pending_key and "e.g." not in lower and "select" not in lower and lower != "*":
                cleaned = line.replace("*", "").strip()
                if cleaned:
                    fields[pending_key] = cleaned
                    pending_key = None
            continue
        if ":" in line:
            fields[key] = _value_after_colon(line)
            pending_key = None
        else:
            pending_key = key
    return fields


def _codes(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()] if raw else []


async def _create_patient(db, message: str) -> tuple[str, dict | None]:
    f = parse_patient_fields(message)
    if not (f["name"] and f["diagnosis"]):
        return PATIENT_FORMAT_HINT, None
    patient = Patient(
        name=f["name"],
        date_of_birth=f["date_of_birth"] or "Unknown",
        admission_date=f["admission_date"] or utcnow().strftime("%Y-%m-%d"),
        room_number=f["room_number"] or "TBD",
        diagnosis=f["diagnosis"],
        attending_physician=f["attending_physician"] or "Unassigned",
    )
    db.add(patient)
    await db.flush()
    return (f"✅ Successfully admitted patient {patient.name} to room {patient.room_number}.",
            {"patients": [patient.to_dict()]})


async def _submit_claim(db, message: str, user: CurrentUser) -> tuple[str, dict | None]:
    f = parse_claim_fields(message)
    if not (f["insurer_name"] and f["billed_amount"]):
        return "Please provide the minimum claim details (Insurer Name, Billed Amount, etc.)", None
    try:
        billed = float(f["billed_amount"].replace("$", "").replace(",", ""))
    except ValueError:
        billed = 0.0

    patients = list(await db.scalars(select(Patient).order_by(Patient.created_at)))
    patient = None
    if f["patient"]:
        # Names are encrypted at rest, so match after decryption rather than with SQL ILIKE.
        needle = f["patient"].lower()
        patient = next((p for p in patients if needle in (p.name or "").lower()), None)
    if not patient:
        patient = patients[0] if patients else None
    if not patient:
        return "There are no patients in the system to attach this claim to.", None

    claim = InsuranceClaim(
        patient_id=patient.patient_id,
        insurer_name=f["insurer_name"],
        policy_number=f["policy_number"],
        diagnosis_codes=_codes(f["diagnosis_codes"]),
        procedure_codes=_codes(f["procedure_codes"]),
        billed_amount=billed,
        created_by=user.user_id,
        submitted_at=utcnow(),
    )
    db.add(claim)
    await db.flush()
    return (f"✅ Successfully submitted new claim to {claim.insurer_name} for ${billed:,.2f}.",
            {"claims": [claim.to_dict()]})


async def _create_task(db, message: str, user: CurrentUser) -> tuple[str, dict | None]:
    lower = message.lower()
    role = "nurse"
    if "physician" in lower or "doctor" in lower:
        role = "physician"
    elif "pharmacist" in lower or "pharmacy" in lower:
        role = "pharmacist"

    to_idx = lower.find(" to ")
    title = message[to_idx + 4:].strip().capitalize() if to_idx != -1 else "New task"

    # "patient 2" refers to the second patient in admission order.
    patients = list(await db.scalars(select(Patient).order_by(Patient.created_at).limit(20)))
    if not patients:
        return "Could not find any patients to assign this task to.", None
    selected = patients[0]
    match = re.search(r"patient\s*(\d+)", lower)
    if match and 0 <= int(match.group(1)) - 1 < len(patients):
        selected = patients[int(match.group(1)) - 1]

    workflow = await discharge_service.active_workflow_for(db, selected.patient_id)
    task = Task(
        patient_id=selected.patient_id,
        workflow_id=workflow.workflow_id if workflow else None,
        title=title,
        description=message,
        category="general",
        assigned_role=role,
        priority=2,
        created_by=user.user_id,
    )
    db.add(task)
    await db.flush()
    await discharge_service.sync_workflow_status(db, task.workflow_id)
    return f"✅ Created new task '{title}' for {role} (Patient: {selected.name}).", {"tasks": [task.to_dict()]}


@router.post("/message")
async def chatbot_message(req: ChatRequest, request: Request, db: DB, user: AuthUser):
    intent = parse_intent(req.message)
    response, data = "", None

    if intent == "create_patient":
        response, data = await _create_patient(db, req.message)
    elif intent == "submit_claim":
        response, data = await _submit_claim(db, req.message, user)
    elif intent == "create_task":
        response, data = await _create_task(db, req.message, user)
    elif intent == "list_patients":
        patients = list(await db.scalars(select(Patient).order_by(Patient.created_at).limit(50)))
        names = [p.name for p in patients]
        response = (f"There are {len(patients)} patients currently in the system: "
                    f"{', '.join(names[:5])}{'...' if len(names) > 5 else '.'}")
        data = {"count": len(patients), "patients": [p.to_dict() for p in patients[:5]]}
    elif intent == "list_tasks":
        tasks = list(await db.scalars(visible_tasks_for(user).order_by(Task.priority).limit(20)))
        response = (f"You have {len(tasks)} assigned task(s). First task: '{tasks[0].title}' — Status: {tasks[0].status}."
                    if tasks else "You have no tasks currently assigned to you.")
        data = {"tasks": [t.to_dict() for t in tasks]}
    elif intent == "analytics":
        n_patients = await db.scalar(select(func.count()).select_from(Patient))
        n_claims = await db.scalar(select(func.count()).select_from(InsuranceClaim))
        billed = await db.scalar(select(func.coalesce(func.sum(InsuranceClaim.billed_amount), 0)))
        response = f"System snapshot: {n_patients} patients, {n_claims} insurance claims, ${float(billed):,.2f} total billed."
    elif intent == "list_claims":
        n_claims = await db.scalar(select(func.count()).select_from(InsuranceClaim))
        response = f"There are {n_claims} insurance claims. Navigate to 💰 Insurance to view and manage them."
        data = {"count": n_claims}
    elif intent == "help":
        response = HELP_TEXT
    else:
        response = ("I didn't quite understand that. Try asking me to 'list patients', 'show my tasks', "
                    "or type 'help' to see what I can do.")

    await write_audit(db, user, f"CHATBOT_{intent.upper()}", "chatbot", None, req.message[:200], request)
    return {"intent": intent, "message": req.message, "response": response, "data": data}
