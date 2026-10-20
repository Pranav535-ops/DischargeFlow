"""Import every module's models so Base.metadata is complete (used by Alembic and tests)."""
from app.ai.models import (
    AIGeneration,
    DischargeSummary,
    DischargeSummaryAddendum,
    DischargeSummaryVersion,
    PatientInstructions,
)
from app.audit.models import AuditChainHead, AuditLog
from app.capacity.models import BedTurnover
from app.chatbot.models import AssistantAction
from app.core.db import Base
from app.discharge.models import DelayReason, Task, Workflow, WorkflowTemplate
from app.downtime.models import DowntimePeriod
from app.escalations.models import Escalation
from app.identity.models import AuthSession, IntegrationClient, RolePermission, Tenant, Unit, User, UserUnit
from app.insurance.models import InsuranceClaim
from app.interop.models import EhrUserLink, HL7Message, HL7ParkedMessage, SmartIssuer, SmartLaunch
from app.notifications.models import Notification
from app.patients.models import AccessGrant, Encounter, Patient

__all__ = [
    "Base",
    "AccessGrant",
    "AIGeneration",
    "AssistantAction",
    "AuditChainHead",
    "AuditLog",
    "AuthSession",
    "BedTurnover",
    "DelayReason",
    "DischargeSummary",
    "DischargeSummaryAddendum",
    "DischargeSummaryVersion",
    "DowntimePeriod",
    "EhrUserLink",
    "Encounter",
    "Escalation",
    "HL7Message",
    "HL7ParkedMessage",
    "InsuranceClaim",
    "IntegrationClient",
    "Notification",
    "Patient",
    "PatientInstructions",
    "RolePermission",
    "SmartIssuer",
    "SmartLaunch",
    "Task",
    "Tenant",
    "Unit",
    "User",
    "UserUnit",
    "Workflow",
    "WorkflowTemplate",
]
