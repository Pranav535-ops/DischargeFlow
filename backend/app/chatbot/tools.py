"""Tools the assistant can call: thin wrappers over the same service functions the HTTP API uses.

Every call goes through the permission its HTTP endpoint requires (`ensure_permission`, the check behind
`requires()`) and writes an audit entry. Tools that change data never run from the model: `propose` stores them as
AssistantAction rows, and they run only when the user confirms (router.confirm_action).
Patients are referred to by de-identification tokens ([PATIENT_1]); tool results are de-identified before they are
returned to the model.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.deid import Deidentifier
from app.ai.schemas import inline_refs
from app.core.security import CurrentUser
from app.discharge import service as discharge_service
from app.discharge.models import Task, Workflow
from app.identity.permissions import ROLES
from app.insurance import service as insurance_service
from app.insurance.models import InsuranceClaim
from app.patients.models import PATIENT_STATUSES, Patient

MAX_CONTEXT_PATIENTS = 500


class ToolInputError(Exception):
    pass


@dataclass
class ToolContext:
    db: AsyncSession
    user: CurrentUser
    request: Optional[Request]
    deid: Deidentifier
    patient_ids: dict[str, str] = field(default_factory=dict)  # token -> patient_id
    patient_tokens: dict[str, str] = field(default_factory=dict)  # patient_id -> token

    @classmethod
    async def build(cls, db: AsyncSession, user: CurrentUser, request: Optional[Request]) -> "ToolContext":
        """Tokens for every patient the user can see. Names are encrypted, so this loads and maps them in Python."""
        ctx = cls(db=db, user=user, request=request, deid=Deidentifier())
        patients = await db.scalars(select(Patient).order_by(Patient.created_at).limit(MAX_CONTEXT_PATIENTS))
        for p in patients:
            if p.gdpr_erased:
                continue
            tok = ctx.deid.add_person("PATIENT", p.name, identity=p.patient_id)
            ctx.patient_ids[tok] = p.patient_id
            ctx.patient_tokens[p.patient_id] = tok
        return ctx

    def token_for(self, patient_id: str) -> str:
        return self.patient_tokens.get(patient_id, "[PATIENT_UNKNOWN]")

    def resolve_patient(self, ref: str) -> str:
        ref = (ref or "").strip()
        if ref in self.patient_ids:
            return self.patient_ids[ref]
        if ref in self.patient_tokens:
            return ref
        raise ToolInputError(f"Unknown patient reference {ref!r}; use a token such as [PATIENT_1] from list_patients.")


@dataclass
class ToolOutput:
    model_data: dict  # de-identified, shown to the model
    ui_data: Optional[dict] = None  # real records for the chat UI, never sent to the model
    resource_id: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    permission: Optional[str]  # the same permission the matching HTTP endpoint requires
    writes: bool
    run: Callable[[ToolContext, Any], Awaitable[ToolOutput]]
    # Human-readable description of a proposed write, for the confirmation card (re-identified).
    describe: Optional[Callable[[ToolContext, Any], str]] = None

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": inline_refs(self.args.model_json_schema())}


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- reads ----------

class ListPatientsArgs(_Args):
    status: Optional[Literal[PATIENT_STATUSES]] = Field(default=None, description="Filter by patient status")


async def list_patients(ctx: ToolContext, args: ListPatientsArgs) -> ToolOutput:
    stmt = select(Patient).order_by(Patient.created_at).limit(50)
    if args.status:
        stmt = stmt.where(Patient.status == args.status)
    patients = [p for p in await ctx.db.scalars(stmt) if not p.gdpr_erased]
    items = [{"patient": ctx.token_for(p.patient_id), "status": p.status} for p in patients]
    return ToolOutput({"count": len(items), "items": items}, {"count": len(items),
                                                              "patients": [p.to_dict() for p in patients[:5]]})


class PatientRefArgs(_Args):
    patient_ref: str = Field(description="Patient token, e.g. [PATIENT_1]")


async def get_patient(ctx: ToolContext, args: PatientRefArgs) -> ToolOutput:
    patient = await ctx.db.get(Patient, ctx.resolve_patient(args.patient_ref))
    workflow = await discharge_service.active_workflow_for(ctx.db, patient.patient_id)
    data = {"patient": ctx.token_for(patient.patient_id), "status": patient.status,
            "diagnosis": ctx.deid.redact(patient.diagnosis), "discharge_workflow": None}
    if workflow:
        data["discharge_workflow"] = {"status": workflow.status,
                                      "open_tasks": await discharge_service.open_task_count(ctx.db, workflow.workflow_id)}
    return ToolOutput(data, {"patients": [patient.to_dict()]}, patient.patient_id)


async def list_my_tasks(ctx: ToolContext, args: _Args) -> ToolOutput:
    tasks = list(await ctx.db.scalars(
        discharge_service.visible_tasks_for(ctx.user).where(Task.status != "completed").order_by(Task.priority, Task.created_at).limit(20)))
    items = [{"task_id": t.task_id, "title": ctx.deid.redact(t.title), "status": t.status, "priority": t.priority,
              "patient": ctx.token_for(t.patient_id)} for t in tasks]
    return ToolOutput({"count": len(items), "items": items}, {"tasks": [t.to_dict() for t in tasks]})


async def get_dashboard_stats(ctx: ToolContext, args: _Args) -> ToolOutput:
    db = ctx.db
    by_status = dict((await db.execute(select(Workflow.status, func.count()).group_by(Workflow.status))).all())
    return ToolOutput({
        "patients": await db.scalar(select(func.count()).select_from(Patient)),
        "active_workflows": by_status.get("in_progress", 0),
        "workflows_pending_approval": by_status.get("pending_approval", 0),
        "completed_discharges": by_status.get("completed", 0),
        "insurance_claims": await db.scalar(select(func.count()).select_from(InsuranceClaim)),
        "total_billed": float(await db.scalar(select(func.coalesce(func.sum(InsuranceClaim.billed_amount), 0)))),
    })


class ListClaimsArgs(_Args):
    status: Optional[Literal["pending", "submitted", "accepted", "denied", "paid"]] = None


async def list_claims(ctx: ToolContext, args: ListClaimsArgs) -> ToolOutput:
    stmt = select(InsuranceClaim).order_by(InsuranceClaim.created_at.desc()).limit(20)
    if args.status:
        stmt = stmt.where(InsuranceClaim.status == args.status)
    claims = list(await ctx.db.scalars(stmt))
    items = [{"claim_id": c.claim_id, "patient": ctx.token_for(c.patient_id), "insurer_name": c.insurer_name,
              "status": c.status, "billed_amount": c.billed_amount} for c in claims]
    return ToolOutput({"count": len(items), "items": items}, {"claims": [c.to_dict() for c in claims]})


# ---------- writes (run only after confirmation) ----------

class CreateTaskArgs(_Args):
    patient_ref: str = Field(description="Patient token, e.g. [PATIENT_1]")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    assigned_role: Literal[ROLES] = "nurse"
    priority: int = Field(default=2, ge=1, le=3, description="1 high, 2 medium, 3 low")


async def create_task(ctx: ToolContext, args: dict) -> ToolOutput:
    task = await discharge_service.create_task(
        ctx.db, ctx.user, patient_id=args["patient_id"], title=args["title"], description=args["description"],
        category="general", assigned_role=args["assigned_role"], priority=args["priority"])
    return ToolOutput({"message": f"Created task '{task.title}' for {task.assigned_role}."},
                      {"tasks": [task.to_dict()]}, task.task_id)


class UpdateTaskArgs(_Args):
    task_id: str
    status: Literal["pending", "in_progress", "completed"]


async def update_task_status(ctx: ToolContext, args: dict) -> ToolOutput:
    task = await discharge_service.update_task(ctx.db, ctx.user, args["task_id"], status=args["status"])
    return ToolOutput({"message": f"Task '{task.title}' is now {task.status}."}, {"tasks": [task.to_dict()]},
                      task.task_id)


async def initiate_discharge(ctx: ToolContext, args: dict) -> ToolOutput:
    out = await discharge_service.initiate_discharge(ctx.db, args["patient_id"], ctx.user)
    return ToolOutput({"message": f"Discharge started with {out['tasks_created']} standard tasks."}, out,
                      out["workflow"]["workflow_id"])


class SubmitClaimArgs(_Args):
    patient_ref: str
    insurer_name: str = Field(min_length=1, max_length=200)
    policy_number: str = Field(default="", max_length=100)
    billed_amount: float = Field(gt=0)
    diagnosis_codes: list[str] = Field(default_factory=list)
    procedure_codes: list[str] = Field(default_factory=list)


async def submit_claim(ctx: ToolContext, args: dict) -> ToolOutput:
    fields = {k: v for k, v in args.items() if k != "patient_ref"}
    claim = await insurance_service.create_claim(ctx.db, ctx.user, request=ctx.request, **fields)
    return ToolOutput({"message": f"Submitted claim to {claim.insurer_name} for ${claim.billed_amount:,.2f}."},
                      {"claims": [claim.to_dict()]}, claim.claim_id)


async def _patient_name(ctx: ToolContext, patient_id: str) -> str:
    patient = await ctx.db.get(Patient, patient_id)
    return patient.name if patient else patient_id


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_patients", "List patients (tokens and status), optionally filtered by status.",
         ListPatientsArgs, None, False, list_patients),
    Tool("get_patient", "Get one patient's status, diagnosis and active discharge workflow.",
         PatientRefArgs, None, False, get_patient),
    Tool("list_my_tasks", "List open tasks assigned to the signed-in user or their role.",
         _Args, None, False, list_my_tasks),
    Tool("get_dashboard_stats", "Counts of patients, discharge workflows and insurance claims.",
         _Args, None, False, get_dashboard_stats),
    Tool("list_claims", "List recent insurance claims, optionally filtered by status.",
         ListClaimsArgs, None, False, list_claims),
    Tool("create_task", "Propose a new task for a patient. Requires the user's confirmation before it is created.",
         CreateTaskArgs, "task:create", True, create_task),
    Tool("update_task_status", "Propose changing a task's status. Requires the user's confirmation.",
         UpdateTaskArgs, None, True, update_task_status),
    Tool("initiate_discharge", "Propose starting the discharge workflow for a patient. Requires confirmation.",
         PatientRefArgs, "workflow:manage", True, initiate_discharge),
    Tool("submit_claim", "Propose submitting an insurance claim for a patient. Requires confirmation.",
         SubmitClaimArgs, None, True, submit_claim),
]}


def tool_definitions() -> list[dict]:
    return [t.definition() for t in TOOLS.values()]


def prepare_write(ctx: ToolContext, tool: Tool, raw_input: dict) -> dict:
    """Validate a proposed write and turn it into stored arguments: patient ids resolved, text re-identified."""
    args = tool.args.model_validate(raw_input).model_dump()
    if "patient_ref" in args:
        args["patient_id"] = ctx.resolve_patient(args.pop("patient_ref"))
    return ctx.deid.reidentify_obj(args)


async def describe_write(ctx: ToolContext, tool_name: str, args: dict) -> str:
    patient = await _patient_name(ctx, args["patient_id"]) if "patient_id" in args else None
    if tool_name == "create_task":
        return (f"Create task \"{args['title']}\" for the {args['assigned_role']} team, patient {patient} "
                f"(priority {args['priority']})")
    if tool_name == "update_task_status":
        task = await ctx.db.get(Task, args["task_id"])
        return f"Mark task \"{task.title if task else args['task_id']}\" as {args['status']}"
    if tool_name == "initiate_discharge":
        return f"Start the discharge workflow for {patient}"
    if tool_name == "submit_claim":
        return f"Submit a claim to {args['insurer_name']} for ${args['billed_amount']:,.2f} (patient {patient})"
    return f"{tool_name} {json.dumps(args)}"


def as_tool_data(name: str, data: dict) -> str:
    """Wrap a tool result as data. '</' is escaped so record text cannot close the wrapper early."""
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return f'<tool_data tool="{name}">{payload}</tool_data>'


def http_error_message(e: HTTPException) -> str:
    return str(e.detail) if isinstance(e.detail, str) else "request failed"
