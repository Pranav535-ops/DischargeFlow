"""Workflow templates per disposition (ADR-002 item 9).

A template lists the tasks a discharge to one disposition needs, with prerequisites (a task can't start until they
are completed) and due times in hours from the start of the expected discharge date, in the hospital's time zone.
`initiate_discharge` uses the template for the encounter's planned disposition; without a plan (or an active
template for it) it falls back to the built-in standard checklist.

Migration 0014 seeds DEFAULT_TEMPLATES (a frozen copy) into every tenant; `ensure_default_templates` does the same
for databases built without migrations (tests, a fresh dev seed).
"""
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.core.config import settings
from app.core.db import utcnow
from app.core.ids import new_id
from app.core.security import CurrentUser
from app.discharge.models import DISPOSITIONS, Task, WorkflowTemplate, sla_hours
from app.identity.permissions import ROLES

MAX_TASKS = 40
# The title approving the AI summary completes (ai.router); every default template has a task with it.
SUMMARY_TITLE = "Complete Discharge Summary"


class TemplateTask(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=1000)
    category: str = Field(default="general", pattern=r"^[a-z0-9_]{1,40}$")
    assigned_role: str
    priority: int = Field(default=2, ge=1, le=3)
    # Hours from 00:00 on the expected discharge date (hospital time); negative is the days before. None: the SLA
    # target for the priority after the task is created.
    due_offset_hours: Optional[int] = Field(default=None, ge=-24 * 30, le=24 * 30)
    depends_on: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("assigned_role")
    @classmethod
    def _role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"assigned_role must be one of {', '.join(ROLES)}")
        return value


class TemplateIn(BaseModel):
    disposition: str
    name: str = Field(min_length=1, max_length=200)
    active: bool = True
    tasks: list[TemplateTask] = Field(min_length=1, max_length=MAX_TASKS)

    @field_validator("disposition")
    @classmethod
    def _disposition(cls, value: str) -> str:
        if value not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {', '.join(DISPOSITIONS)}")
        return value

    @model_validator(mode="after")
    def _dependencies(self) -> "TemplateIn":
        keys = [t.key for t in self.tasks]
        if len(set(keys)) != len(keys):
            raise ValueError("task keys must be unique")
        graph = {t.key: t.depends_on for t in self.tasks}
        for task in self.tasks:
            for dep in task.depends_on:
                if dep == task.key:
                    raise ValueError(f"task {task.key} depends on itself")
                if dep not in graph:
                    raise ValueError(f"task {task.key} depends on unknown task {dep}")
        if _has_cycle(graph):
            raise ValueError("task dependencies form a cycle")
        return self


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(visit(dep) for dep in graph[node]):
            return True
        state[node] = 2
        return False

    return any(visit(node) for node in graph)


def _task(key, title, description, category, role, priority, offset, depends_on=()):
    return {"key": key, "title": title, "description": description, "category": category, "assigned_role": role,
            "priority": priority, "due_offset_hours": offset, "depends_on": list(depends_on)}


_SUMMARY = _task("summary", SUMMARY_TITLE, "Generate and approve AI discharge summary", "documentation",
                 "physician", 1, 10)
_MED_REVIEW = _task("med_review", "Final Medication Review", "Review and reconcile all medications", "medication",
                    "pharmacist", 1, 9)
_MEDS = _task("meds", "Prepare Discharge Medications", "Prepare and package medications for patient", "medication",
              "pharmacist", 2, 11, ["med_review"])
_EDUCATION = _task("education", "Patient Education", "Provide discharge instructions and education materials",
                   "education", "nurse", 2, 11, ["med_review"])
_FOLLOW_UP = _task("follow_up", "Schedule Follow-up Appointments", "Schedule necessary follow-up appointments",
                   "follow_up", "nurse", 2, 10)
_VITALS = _task("vitals", "Final Vitals Check", "Record final vital signs before discharge", "documentation",
                "nurse", 1, 11)
_IV = _task("iv", "Remove IV and Equipment", "Remove IV lines and return equipment", "documentation", "nurse", 2, 12,
            ["vitals"])


def _facility(kind: str, placement_title: str) -> list[dict]:
    """Transfer to a facility: placement and authorisation the day(s) before, packet and transport on the day."""
    return [
        _task("placement", placement_title, f"Referral sent and acceptance confirmed by the {kind}", "placement",
              "nurse", 1, -48),
        _task("prior_auth", "Insurance Prior Authorization", f"Obtain payer authorization for the {kind} stay",
              "insurance", "nurse", 1, -24, ["placement"]),
        _SUMMARY, _MED_REVIEW, _MEDS, _VITALS, _IV,
        _task("transfer_packet", "Send Transfer Packet", "Summary, medication list and orders sent to the facility",
              "documentation", "nurse", 1, 11, ["summary", "med_review", "placement"]),
        _task("transport", "Arrange Transport", "Book transport to the facility for the discharge time", "transport",
              "nurse", 2, 10, ["prior_auth"]),
    ]


# Default templates. Migration 0014 holds a frozen copy (tests check they agree when introduced); change them for a
# hospital by editing templates in the admin screen, not here.
DEFAULT_TEMPLATES: list[dict] = [
    {"disposition": "home", "name": "Home (self-care)", "tasks": [
        _SUMMARY, _MED_REVIEW, _MEDS, _EDUCATION, _FOLLOW_UP,
        _task("billing", "Billing Clearance", "Verify insurance and billing clearance", "billing", "nurse", 3, 12),
        _VITALS, _IV,
    ]},
    {"disposition": "home_services", "name": "Home with services", "tasks": [
        _task("home_health", "Arrange Home Health Services", "Agency accepted the referral and set a start of care",
              "placement", "nurse", 1, -24),
        _task("equipment", "Order Durable Medical Equipment", "Equipment ordered and delivery confirmed", "equipment",
              "nurse", 2, -24),
        _SUMMARY, _MED_REVIEW, _MEDS,
        _task("education", "Patient and Caregiver Education", "Teach-back on medications, equipment and home care",
              "education", "nurse", 2, 11, ["med_review", "equipment"]),
        _FOLLOW_UP, _VITALS, _IV,
    ]},
    {"disposition": "snf", "name": "Skilled nursing facility", "tasks": _facility("facility", "Secure SNF Placement")},
    {"disposition": "rehab", "name": "Inpatient rehabilitation",
     "tasks": _facility("rehabilitation unit", "Secure Rehab Bed Acceptance")},
    {"disposition": "hospice", "name": "Hospice", "tasks": [
        _task("hospice_referral", "Hospice Referral and Consent", "Hospice accepted; goals of care and consent recorded",
              "placement", "nurse", 1, -24),
        _SUMMARY,
        _task("med_review", "Comfort Medication Plan", "Reconcile medications for comfort care", "medication",
              "pharmacist", 1, 9),
        _task("education", "Family Education", "Teach the family the care and symptom plan", "education", "nurse", 2,
              11, ["med_review", "hospice_referral"]),
        _task("transport", "Arrange Transport", "Book transport to home or the hospice", "transport", "nurse", 2, 10,
              ["hospice_referral"]),
    ]},
    {"disposition": "ama", "name": "Left against medical advice", "tasks": [
        _task("ama_form", "Document AMA Discussion", "Risks explained, capacity assessed, AMA form signed or refused",
              "documentation", "physician", 1, 0),
        _SUMMARY,
        _task("med_review", "Final Medication Review", "Prescriptions and medication risks for the patient leaving",
              "medication", "pharmacist", 1, 0),
        _task("education", "Return Precautions", "When to come back; follow-up offered", "education", "nurse", 1, 0),
    ]},
]


def hospital_zone() -> ZoneInfo:
    return ZoneInfo(settings.hospital_timezone)


def hospital_today() -> date:
    return utcnow().astimezone(hospital_zone()).date()


def due_at_for(expected: Optional[date], offset_hours: Optional[int], priority: int,
               now: Optional[datetime] = None) -> Optional[datetime]:
    """The due time of a template task: `offset_hours` from the start of the expected discharge date (hospital
    time). None without a date or an offset (the task then gets the SLA target from creation). A task whose due time
    has already passed when it is created gets its priority's SLA target from now instead: nobody could have been on
    time for it."""
    if expected is None or offset_hours is None:
        return None
    now = now or utcnow()
    due = datetime.combine(expected, time(), hospital_zone()) + timedelta(hours=offset_hours)
    return now + timedelta(hours=sla_hours(priority)) if due < now else due


async def template_for(db: AsyncSession, disposition: Optional[str]) -> Optional[WorkflowTemplate]:
    if not disposition:
        return None
    return await db.scalar(select(WorkflowTemplate).where(WorkflowTemplate.disposition == disposition,
                                                          WorkflowTemplate.active.is_(True)))


def tasks_from_template(template: WorkflowTemplate, *, workflow_id: str, patient_id: str,
                        expected: Optional[date]) -> list[Task]:
    """The template's tasks for one workflow, with prerequisites mapped from template keys to task ids."""
    specs = [TemplateTask(**t) for t in template.tasks]
    ids = {spec.key: new_id("task") for spec in specs}
    now = utcnow()
    return [
        Task(task_id=ids[spec.key], workflow_id=workflow_id, patient_id=patient_id, title=spec.title,
             description=spec.description, category=spec.category, assigned_role=spec.assigned_role,
             priority=spec.priority, due_offset_hours=spec.due_offset_hours,
             due_at=due_at_for(expected, spec.due_offset_hours, spec.priority, now),
             depends_on=[ids[d] for d in spec.depends_on] or None)
        for spec in specs
    ]


async def list_templates(db: AsyncSession) -> list[dict]:
    return [t.to_dict() for t in await db.scalars(select(WorkflowTemplate).order_by(WorkflowTemplate.disposition))]


async def create_template(db: AsyncSession, user: CurrentUser, body: TemplateIn,
                          request: Optional[Request] = None) -> WorkflowTemplate:
    if await db.scalar(select(WorkflowTemplate.template_id).where(WorkflowTemplate.disposition == body.disposition)):
        raise HTTPException(status_code=409, detail="A template for this disposition already exists")
    template = WorkflowTemplate(disposition=body.disposition, name=body.name, active=body.active,
                                tasks=[t.model_dump() for t in body.tasks], updated_by=user.user_id)
    db.add(template)
    await db.flush()
    await write_audit(db, user, "CREATE_WORKFLOW_TEMPLATE", "workflow_template", template.template_id,
                      f"disposition={template.disposition} tasks={len(template.tasks)}", request)
    return template


async def update_template(db: AsyncSession, user: CurrentUser, template_id: str, body: TemplateIn,
                          request: Optional[Request] = None) -> WorkflowTemplate:
    """Replace a template's name, tasks and active flag; the version goes up. Workflows already started keep their
    tasks. The disposition can't change (create a template for the other one)."""
    template = await db.get(WorkflowTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if body.disposition != template.disposition:
        raise HTTPException(status_code=400, detail="A template's disposition can't change")
    template.name = body.name
    template.active = body.active
    template.tasks = [t.model_dump() for t in body.tasks]
    template.version += 1
    template.updated_by = user.user_id
    await db.flush()
    await write_audit(db, user, "UPDATE_WORKFLOW_TEMPLATE", "workflow_template", template_id,
                      f"version={template.version} active={template.active} tasks={len(template.tasks)}", request)
    return template


async def ensure_default_templates(db: AsyncSession) -> int:
    """Add the default template for each disposition the current tenant has none for. Returns how many."""
    have = set(await db.scalars(select(WorkflowTemplate.disposition)))
    missing = [t for t in DEFAULT_TEMPLATES if t["disposition"] not in have]
    db.add_all(WorkflowTemplate(disposition=t["disposition"], name=t["name"], tasks=t["tasks"]) for t in missing)
    await db.flush()
    return len(missing)
