"""Import every module's models so Base.metadata is complete (used by Alembic and tests)."""
from app.ai.models import AIGeneration, DischargeSummary, DischargeSummaryAddendum, DischargeSummaryVersion
from app.audit.models import AuditChainHead, AuditLog
from app.chatbot.models import AssistantAction
from app.core.db import Base
from app.discharge.models import Task, Workflow
from app.downtime.models import DowntimePeriod
from app.escalations.models import Escalation
from app.identity.models import AuthSession, IntegrationClient, RolePermission, Tenant, Unit, User, UserUnit
from app.insurance.models import InsuranceClaim
from app.interop.models import HL7Message
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
    "DischargeSummary",
    "DischargeSummaryAddendum",
    "DischargeSummaryVersion",
    "DowntimePeriod",
    "Encounter",
    "Escalation",
    "HL7Message",
    "InsuranceClaim",
    "IntegrationClient",
    "Notification",
    "Patient",
    "RolePermission",
    "Task",
    "Tenant",
    "Unit",
    "User",
    "UserUnit",
    "Workflow",
]
